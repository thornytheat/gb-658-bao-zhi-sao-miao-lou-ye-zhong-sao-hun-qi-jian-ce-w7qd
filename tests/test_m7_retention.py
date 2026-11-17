"""M7 留存与销毁测试。

每条用例使用独立临时库，避免污染默认 data/runs.sqlite3。
"""
import sqlite3

import pytest

import app.modules.m7_retention.store as m7_store
from app.errors import DomainError
from app.modules.m7_retention import service
from app.modules.m7_retention.crypto import sha256_text, sign, verify
from app.modules.m7_retention.ingest import parse_real_manifest

HEADER = "issue,edition,page,file_name,sha256,phash,clarity,header_issue\n"


def row(issue="2026-11-01", page="001", name="a.tif"):
    return f"{issue},01,{page},{name},{'a' * 64},0f0f,90,{issue}\n"



def sample_manifest() -> str:
    return HEADER + row(page="001") + row(page="002", name="b.tif")


# ---------- 接收：真实清单、保留期限、验收凭据 ----------

def test_receive_real_manifest_issues_signed_credential(db_path):
    result = service.receive(request_id="req-1", manifest_text=sample_manifest(), retention_days=15)
    assert result["outcome"] == "ACCEPTED"
    assert result["status"] == "ACTIVE"
    assert result["received_count"] == 2
    assert result["accepted_count"] == 2 and result["quarantined_count"] == 0
    credential = result["credential"]
    assert credential["algorithm"] == "HMAC-SHA256"
    assert verify(credential["payload"], credential["signature"])
    assert credential["payload"]["retain_until"] > result["received_at"]


def test_receive_persists_raw_text_and_quarantine_original_value(db_path):
    dirty = HEADER + row() + "2026-11-01,01,003\n"  # 列数不齐 -> 隔离原值
    result = service.receive(request_id="req-dirty", manifest_text=dirty, retention_days=5)
    assert result["quarantined_count"] == 1
    with m7_store.connect() as db:
        records = db.execute("SELECT line_no, accepted, reason_code, raw_text FROM m7_records").fetchall()
    quarantined = [r for r in records if not r["accepted"]]
    assert len(quarantined) == 1
    assert quarantined[0]["reason_code"] == "COLUMN_COUNT"
    assert "2026-11-01,01,003" in quarantined[0]["raw_text"]


def test_truncated_tail_is_quarantined_not_accepted(db_path):
    truncated = (HEADER + row()).rstrip("\n")
    accepted, bad, tail_complete = parse_real_manifest(truncated)
    assert tail_complete is False
    assert any(q["reason_code"] == "TRUNCATED_TAIL" for q in bad)


def test_bom_header_still_parses(db_path):
    accepted, bad, _ = parse_real_manifest("﻿" + HEADER + row())
    assert len(accepted) == 1 and not bad


def test_negative_retention_rejected(db_path):
    with pytest.raises(DomainError) as exc:
        service.receive(request_id="req-x", manifest_text=sample_manifest(), retention_days=-1)
    assert exc.value.code == "INVALID_RETENTION"


# ---------- 幂等与冲突 ----------

def test_same_request_same_content_is_idempotent(db_path):
    first = service.receive(request_id="req-1", manifest_text=sample_manifest())
    second = service.receive(request_id="req-1", manifest_text=sample_manifest())
    assert second["outcome"] == "REUSED"
    assert first["retention_id"] == second["retention_id"]
    with m7_store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM m7_retentions").fetchone()[0] == 1


def test_same_request_different_content_rejected(db_path):
    service.receive(request_id="req-1", manifest_text=sample_manifest())
    with pytest.raises(DomainError) as exc:
        service.receive(request_id="req-1", manifest_text=HEADER + row(page="009"))
    assert exc.value.status == 409 and exc.value.code == "REQUEST_CONTENT_CONFLICT"


def test_new_request_same_content_returns_existing_retention(db_path):
    first = service.receive(request_id="req-1", manifest_text=sample_manifest())
    second = service.receive(request_id="req-2", manifest_text=sample_manifest())
    assert second["outcome"] == "CONTENT_ALREADY_RETAINED"
    assert second["retention_id"] == first["retention_id"]


# ---------- 销毁：清除原文、保留不可还原依据 ----------

def test_destroy_removes_raw_but_keeps_digest_and_signature(db_path):
    received = service.receive(request_id="req-1", manifest_text=sample_manifest(), retention_days=0)
    result = service.destroy(request_id="req-1")
    assert result["status"] == "DESTROYED" and result["evidence_count"] == 2
    with m7_store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM m7_records").fetchone()[0] == 0
        evidence = db.execute("SELECT * FROM m7_evidence ORDER BY line_no").fetchall()
        retention = db.execute("SELECT status FROM m7_retentions").fetchone()
    assert retention["status"] == "DESTROYED"
    # 只剩摘要、行号、原因码与签名，原文文本无处可查
    assert all(e["raw_digest"] and not e.keys().__contains__("raw_text") for e in evidence)
    payload = {
        "retention_id": received["retention_id"],
        "content_digest": received["content_digest"],
        "line_no": evidence[0]["line_no"],
        "accepted": bool(evidence[0]["accepted"]),
        "reason_code": evidence[0]["reason_code"],
        "raw_digest": evidence[0]["raw_digest"],
        "credential_type": "ROW_VERIFICATION_BASIS",
    }
    assert verify(payload, evidence[0]["row_signature"])


def test_destroy_before_due_is_refused_unless_forced(db_path):
    service.receive(request_id="req-1", manifest_text=sample_manifest(), retention_days=30)
    with pytest.raises(DomainError) as exc:
        service.destroy(request_id="req-1")
    assert exc.value.code == "RETENTION_NOT_DUE"
    forced = service.destroy(request_id="req-1", force=True)
    assert forced["early"] is True and forced["status"] == "DESTROYED"


def test_destroy_reuses_original_credential(db_path):
    received = service.receive(request_id="req-1", manifest_text=sample_manifest(), retention_days=0)
    destroyed = service.destroy(request_id="req-1")
    receipt_cred = destroyed["receipt"]["payload"]["credential"]
    assert receipt_cred["signature"] == received["credential"]["signature"]
    assert verify(destroyed["receipt"]["payload"], destroyed["receipt"]["signature"])


def test_destroy_is_resumable_after_interruption(db_path, monkeypatch):
    service.receive(request_id="req-1", manifest_text=sample_manifest(), retention_days=0)

    real_delete = m7_store.delete_record
    calls = {"n": 0}

    def flaky_delete(db, record_id):
        calls["n"] += 1
        real_delete(db, record_id)
        if calls["n"] == 2:
            raise sqlite3.OperationalError("simulated interruption")

    monkeypatch.setattr(m7_store, "delete_record", flaky_delete)
    with pytest.raises(sqlite3.OperationalError):
        service.destroy(request_id="req-1")

    # 第一行已随事务提交：证据落库、原文删除、检查点推进，均持久化
    with m7_store.connect() as db:
        progress = db.execute("SELECT * FROM m7_destroy_progress").fetchone()
        assert progress["processed"] == 1 and progress["last_line_no"] == 2
        assert db.execute("SELECT COUNT(*) FROM m7_evidence").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM m7_records").fetchone()[0] == 1

    monkeypatch.setattr(m7_store, "delete_record", real_delete)
    # 重试沿用原验收凭据，从检查点继续，不重复处理已完成行
    again = service.destroy(request_id="req-1")
    assert again["status"] == "DESTROYED" and again["resumed"] is True
    with m7_store.connect() as db:
        progress = db.execute("SELECT * FROM m7_destroy_progress").fetchone()
        assert progress["processed"] == 2
        assert db.execute("SELECT COUNT(*) FROM m7_records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM m7_evidence").fetchone()[0] == 2


def test_destroy_idempotent_after_completion(db_path):
    service.receive(request_id="req-1", manifest_text=sample_manifest(), retention_days=0)
    first = service.destroy(request_id="req-1")
    second = service.destroy(request_id="req-1")
    assert second["outcome"] == "ALREADY_DESTROYED"
    assert second["receipt"]["payload"]["credential"]["signature"] == \
        first["receipt"]["payload"]["credential"]["signature"]


# ---------- 销毁与导入并发 ----------

def test_destroyed_manifest_cannot_re_enter_store(db_path):
    service.receive(request_id="req-1", manifest_text=sample_manifest(), retention_days=0)
    service.destroy(request_id="req-1")
    again = service.receive(request_id="req-2", manifest_text=sample_manifest())
    assert again["outcome"] == "ALREADY_DESTROYED"
    with m7_store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM m7_retentions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM m7_records").fetchone()[0] == 0


def test_destroyed_receipt_returned_for_same_content_new_request(db_path):
    service.receive(request_id="req-1", manifest_text=sample_manifest(), retention_days=0)
    service.destroy(request_id="req-1")
    result = service.receive(request_id="req-new", manifest_text=sample_manifest())
    assert result["status"] == "DESTROYED" and result["received_count"] == 2


# ---------- 核验 ----------

def test_verify_original_matches_after_destruction_without_persisting(db_path):
    manifest = sample_manifest()
    received = service.receive(request_id="req-1", manifest_text=manifest, retention_days=0)
    service.destroy(request_id="req-1")
    result = service.verify_original(
        request_id="req-1", retention_id=received["retention_id"], manifest_text=manifest
    )
    assert result["verified"] is True
    assert result["status"] == "DESTROYED" and result["original_persisted"] is False
    assert all(line["digest_match"] for line in result["line_results"])
    # 核验提交的原件没有落库
    with m7_store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM m7_records").fetchone()[0] == 0


def test_verify_rejects_tampered_original(db_path):
    manifest = sample_manifest()
    received = service.receive(request_id="req-1", manifest_text=manifest, retention_days=0)
    service.destroy(request_id="req-1")
    tampered = HEADER + row(page="001") + row(page="002", name="HACKED.tif")
    result = service.verify_original(
        request_id="req-1", retention_id=received["retention_id"], manifest_text=tampered
    )
    assert result["verified"] is False
    assert result["content_digest_match"] is False


def test_verify_while_active_also_uses_digests(db_path):
    manifest = sample_manifest()
    received = service.receive(request_id="req-1", manifest_text=manifest, retention_days=30)
    result = service.verify_original(
        request_id="req-1", retention_id=received["retention_id"], manifest_text=manifest
    )
    # 留存中尚无销毁证据行，逐行依据不可用 -> verified 为 False 但凭据本身有效
    assert result["credential_valid"] is True and result["rows_match"] is False


def test_verify_request_retention_mismatch_rejected(db_path):
    received = service.receive(request_id="req-1", manifest_text=sample_manifest(), retention_days=0)
    with pytest.raises(DomainError) as exc:
        service.verify_original(
            request_id="req-other", retention_id=received["retention_id"],
            manifest_text=sample_manifest(),
        )
    assert exc.value.code == "UNKNOWN_REQUEST"


# ---------- 回读 ----------

def test_read_status_reports_original_received_count(db_path):
    received = service.receive(request_id="req-1", manifest_text=sample_manifest(), retention_days=0)
    status = service.read_status(received["retention_id"])
    assert status["received_count"] == 2
    assert status["raw_text_available"] is True
    service.destroy(request_id="req-1")
    after = service.read_status(received["retention_id"])
    assert after["status"] == "DESTROYED"
    assert after["received_count"] == 2  # 原接收数量仍可追溯
    assert after["raw_text_available"] is False


# ---------- 凭据原语 ----------

def test_signature_tamper_detection():
    payload = {"a": 1, "b": "x"}
    signature = sign(payload)
    assert verify(payload, signature)
    assert not verify({**payload, "b": "y"}, signature)
    assert not verify(payload, signature[:-1] + ("0" if signature[-1] != "0" else "1"))


def test_row_digest_is_irreversible_sha256():
    text = row()
    digest = sha256_text(text)
    assert len(digest) == 64 and digest != text


def test_quarantined_rows_keep_reason_code_in_evidence_and_verify(db_path):
    dirty = HEADER + row() + "2026-11-01,01,003\n"  # 隔离原值
    received = service.receive(request_id="req-q", manifest_text=dirty, retention_days=0)
    assert received["quarantined_count"] == 1
    service.destroy(request_id="req-q", force=True)
    result = service.verify_original(
        request_id="req-q", retention_id=received["retention_id"], manifest_text=dirty
    )
    assert result["verified"] is True
    quarantined_lines = [line for line in result["line_results"] if line["reason_code"]]
    assert len(quarantined_lines) == 1
    assert quarantined_lines[0]["reason_code"] == "COLUMN_COUNT"
    assert quarantined_lines[0]["digest_match"] is True


def test_empty_manifest_is_quarantined_and_destroyable(db_path):
    received = service.receive(request_id="req-empty", manifest_text="", retention_days=0)
    assert received["received_count"] == 1 and received["accepted_count"] == 0
    destroyed = service.destroy(request_id="req-empty", force=True)
    assert destroyed["evidence_count"] == 1 and destroyed["status"] == "DESTROYED"
    status = service.read_status(received["retention_id"])
    assert status["received_count"] == 1 and status["raw_text_available"] is False
    verified = service.verify_original(
        request_id="req-empty", retention_id=received["retention_id"], manifest_text=""
    )
    assert verified["verified"] is True
    assert verified["line_results"][0]["reason_code"] == "EMPTY_FILE"


def test_credential_respects_configured_secret():
    from app.modules.m7_retention.crypto import sign as sign_with
    payload = {"k": "v"}
    sig_default = sign_with(payload)
    # 显式传不同密钥得到不同签名，且各自只能自验
    sig_other = sign_with(payload, secret="another-secret")
    assert sig_default != sig_other
    assert verify(payload, sig_default)
    assert verify(payload, sig_other, secret="another-secret")
    assert not verify(payload, sig_other)
