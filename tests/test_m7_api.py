"""M7 HTTP 接口与真实并发测试。"""
import threading

import pytest
from fastapi.testclient import TestClient

import app.modules.m7_retention.store as m7_store
from app.main import app
from app.modules.m7_retention import service

HEADER = "issue,edition,page,file_name,sha256,phash,clarity,header_issue\n"


def manifest() -> str:
    return HEADER + "2026-11-01,01,001,a.tif," + "a" * 64 + ",0f0f,90,2026-11-01\n"


@pytest.fixture()
def client(db_path):
    return TestClient(app)


def test_page_and_full_http_lifecycle(client):
    page = client.get("/retention")
    assert page.status_code == 200 and "panel" in page.text

    received = client.post("/api/retention/receive", json={
        "request_id": "http-1", "manifest_text": manifest(), "retention_days": 0,
    }).json()
    assert received["outcome"] == "ACCEPTED"
    retention_id = received["retention_id"]

    status = client.get(f"/api/retention/{retention_id}").json()
    assert status["received_count"] == 1 and status["status"] == "ACTIVE"

    destroyed = client.post("/api/retention/destroy", json={
        "request_id": "http-1", "force": True,
    }).json()
    assert destroyed["status"] == "DESTROYED"

    verified = client.post("/api/retention/verify", json={
        "request_id": "http-1", "retention_id": retention_id,
        "manifest_text": manifest(),
    }).json()
    assert verified["verified"] is True and verified["original_persisted"] is False


def test_same_request_different_content_returns_409(client):
    client.post("/api/retention/receive", json={
        "request_id": "http-2", "manifest_text": manifest(),
    })
    other = HEADER + "2026-11-01,01,009,z.tif," + "b" * 64 + ",0f0f,90,2026-11-01\n"
    response = client.post("/api/retention/receive", json={
        "request_id": "http-2", "manifest_text": other,
    })
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REQUEST_CONTENT_CONFLICT"


def test_unknown_request_destroy_returns_404(client):
    response = client.post("/api/retention/destroy", json={"request_id": "nope"})
    assert response.status_code == 404


def test_destroyed_manifest_re_import_returns_receipt(client):
    client.post("/api/retention/receive", json={
        "request_id": "http-3", "manifest_text": manifest(), "retention_days": 0,
    })
    client.post("/api/retention/destroy", json={"request_id": "http-3", "force": True})
    again = client.post("/api/retention/receive", json={
        "request_id": "http-4", "manifest_text": manifest(),
    }).json()
    assert again["outcome"] == "ALREADY_DESTROYED"


def test_concurrent_receive_same_content_single_row(client):
    outcomes = []

    def worker(rid):
        result = client.post("/api/retention/receive", json={
            "request_id": rid, "manifest_text": manifest(), "retention_days": 5,
        }).json()
        outcomes.append(result["outcome"])

    threads = [threading.Thread(target=worker, args=(f"c-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ACCEPTED") == 1
    assert set(outcomes) <= {"ACCEPTED", "CONTENT_ALREADY_RETAINED"}
    with m7_store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM m7_retentions").fetchone()[0] == 1
        # 只由成功落库的一方写入一次原文
        assert db.execute("SELECT COUNT(*) FROM m7_records").fetchone()[0] == 1


def test_concurrent_destroy_and_receive_old_manifest_stays_destroyed(client):
    client.post("/api/retention/receive", json={
        "request_id": "seed", "manifest_text": manifest(), "retention_days": 0,
    })
    errors = []

    def destroyer():
        try:
            service.destroy(request_id="seed", force=True)
        except Exception as exc:  # pragma: no cover - 竞争失败需要显式暴露
            errors.append(exc)

    def importer():
        try:
            service.receive(request_id="late-import", manifest_text=manifest())
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t1 = threading.Thread(target=destroyer)
    t2 = threading.Thread(target=importer)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert not errors

    with m7_store.connect() as db:
        row = db.execute("SELECT status FROM m7_retentions").fetchone()
        assert row["status"] == "DESTROYED"
        assert db.execute("SELECT COUNT(*) FROM m7_records").fetchone()[0] == 0
