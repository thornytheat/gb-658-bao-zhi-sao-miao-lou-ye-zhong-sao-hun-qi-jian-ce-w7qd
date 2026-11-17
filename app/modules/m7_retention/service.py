"""M7 留存与销毁领域服务。

状态机：ACTIVE -> DESTROYING -> DESTROYED（DESTROYING 是中断恢复态，
重新调用销毁即沿检查点继续；重试始终沿用首次签发的验收凭据）。
"""
from datetime import datetime, timedelta, timezone

from app.config import DEFAULT_RETENTION_DAYS
from app.errors import DomainError
from . import store
from .credential import (
    destruction_receipt,
    issue_credential,
    row_evidence_payload,
)
from .crypto import sha256_bytes, sign, verify
from .ingest import parse_real_manifest

RETENTION_PREFIX = "ret"


def _retention_id(content_digest: str) -> str:
    return f"{RETENTION_PREFIX}-{content_digest[:16]}"


def _retain_until(received_at: datetime, retention_days: int) -> str:
    if retention_days < 0:
        raise DomainError("INVALID_RETENTION", "保留期限不能为负数")
    return (received_at + timedelta(days=retention_days)).isoformat()


def receive(*, request_id: str, manifest_text: str,
            retention_days: int = DEFAULT_RETENTION_DAYS) -> dict:
    """接收真实清单：登记保留期限并签发验收凭据。

    幂等与冲突规则：
    - 同 request_id + 同内容：原样返回，不重复落库；
    - 同 request_id + 不同内容：拒绝（409）；
    - 不同 request_id + 同内容且仍在留存：返回既有凭据（reused）；
    - 不同 request_id + 同内容但已销毁：旧清单不得重新落库，返回已销毁回执。
    """
    if not request_id or not request_id.strip():
        raise DomainError("MISSING_REQUEST_ID", "request_id 不能为空")
    payload = manifest_text.encode("utf-8")
    content_digest = sha256_bytes(payload)
    retention_id = _retention_id(content_digest)
    stamp = store.now_iso()
    received_at_dt = datetime.now(timezone.utc)
    retain_until = _retain_until(received_at_dt, retention_days)

    accepted_rows, quarantined_rows, tail_complete = parse_real_manifest(manifest_text)
    received_count = len(accepted_rows) + len(quarantined_rows)

    store.init_db()
    with store.connect() as db:
        ledger = store.find_ledger(db, request_id)
        if ledger is not None and ledger["content_digest"] != content_digest:
            raise DomainError(
                "REQUEST_CONTENT_CONFLICT",
                "同一请求 ID 已绑定不同内容，拒绝受理",
                status=409,
            )

        existing = store.find_retention_by_digest(db, content_digest)
        if existing is not None:
            if existing["status"] == "DESTROYED":
                return {
                    "outcome": "ALREADY_DESTROYED",
                    "retention_id": existing["retention_id"],
                    "status": "DESTROYED",
                    "received_count": existing["received_count"],
                    "content_digest": content_digest,
                    "message": "该清单已到期销毁，原文不得重新落库",
                }
            credential = _credential_from_row(existing)
            return {
                "outcome": "REUSED" if ledger is not None else "CONTENT_ALREADY_RETAINED",
                "retention_id": existing["retention_id"],
                "status": existing["status"],
                "received_count": existing["received_count"],
                "accepted_count": existing["accepted_count"],
                "quarantined_count": existing["quarantined_count"],
                "received_at": existing["received_at"],
                "retain_until": existing["retain_until"],
                "content_digest": content_digest,
                "credential": credential,
                "tail_complete": tail_complete,
            }

        created = store.insert_retention(
            db, retention_id, content_digest, request_id, received_count,
            len(accepted_rows), len(quarantined_rows), stamp, retain_until,
        )
        if not created:
            # 并发导入：同内容已被另一事务落库，旧清单不重复入库。
            row = store.find_retention_by_digest(db, content_digest)
            return {
                "outcome": "CONTENT_ALREADY_RETAINED",
                "retention_id": row["retention_id"],
                "status": row["status"],
                "received_count": row["received_count"],
                "content_digest": content_digest,
                "credential": _credential_from_row(row),
                "tail_complete": tail_complete,
            }

        for item in accepted_rows:
            store.insert_record(db, retention_id, item["line_no"], True, None,
                                item["raw_text"], item["raw_digest"])
        for item in quarantined_rows:
            # 隔离行（含空文件 line_no=0）同样留存原值，销毁后转为带原因码的核验依据
            store.insert_record(db, retention_id, item["line_no"], False,
                                item["reason_code"], item["raw_text"],
                                item["raw_digest"])
        store.bind_request(db, request_id, content_digest, retention_id, stamp)

        row = store.get_retention(db, retention_id)
        credential = _credential_from_row(row)

    return {
        "outcome": "ACCEPTED",
        "retention_id": retention_id,
        "status": "ACTIVE",
        "received_count": received_count,
        "accepted_count": len(accepted_rows),
        "quarantined_count": len(quarantined_rows),
        "received_at": stamp,
        "retain_until": retain_until,
        "content_digest": content_digest,
        "credential": credential,
        "tail_complete": tail_complete,
        "quarantine_reasons": sorted({q["reason_code"] for q in quarantined_rows}),
    }


def _credential_from_row(row) -> dict:
    return issue_credential(
        retention_id=row["retention_id"],
        content_digest=row["content_digest"],
        request_id=row["request_id"],
        received_count=row["received_count"],
        accepted_count=row["accepted_count"],
        quarantined_count=row["quarantined_count"],
        received_at=row["received_at"],
        retain_until=row["retain_until"],
    )


def destroy(*, request_id: str, retention_id: str | None = None,
            force: bool = False) -> dict:
    """销毁原文与隔离原值，仅保留不可还原的核验依据。

    - 必须凭原请求 ID 调用，且 ID 与留存绑定一致；
    - 未到 retain_until 默认拒绝，force=True 可提前销毁并在结果中标注；
    - 中断恢复：行级检查点之后继续，已销毁行不重复处理，凭据不变。
    """
    store.init_db()
    # 校验阶段只读，独立短事务。
    with store.connect() as db:
        ledger = store.find_ledger(db, request_id)
        if ledger is None:
            raise DomainError("UNKNOWN_REQUEST", "请求 ID 未登记，无法销毁", status=404)
        target_id = retention_id or ledger["retention_id"]
        if ledger["retention_id"] != target_id:
            raise DomainError(
                "REQUEST_RETENTION_MISMATCH",
                "请求 ID 与留存记录不匹配，拒绝销毁",
                status=409,
            )
        row = store.get_retention(db, target_id)
        if row is None:
            raise DomainError("UNKNOWN_RETENTION", "留存记录不存在", status=404)
        if row["status"] == "DESTROYED":
            receipt = destruction_receipt(
                credential=_credential_from_row(row),
                destroyed_at=store.get_progress(db, target_id)["updated_at"],
                evidence_count=len(store.list_evidence(db, target_id)),
                resumed=True,
                status="DESTROYED",
            )
            return {"outcome": "ALREADY_DESTROYED", "retention_id": target_id,
                    "status": "DESTROYED", "receipt": receipt}
        now_dt = datetime.now(timezone.utc)
        due = now_dt >= datetime.fromisoformat(row["retain_until"])
        if not due and not force:
            raise DomainError(
                "RETENTION_NOT_DUE",
                f"保留期未到（retain_until={row['retain_until']}）",
                status=409,
            )
        content_digest = row["content_digest"]
        resumed = row["status"] == "DESTROYING"

    # 销毁阶段：每行独立事务并立即提交，保证中断后检查点真实持久，
    # 重试时只处理检查点之后的行。
    with store.connect() as db:
        stamp = store.now_iso()
        store.mark_status(db, target_id, "DESTROYING")
        progress = store.ensure_progress(db, target_id, stamp)
        last_line = progress["last_line_no"]
        processed = progress["processed"]
        db.commit()

        pending = store.iter_pending_records(db, target_id, last_line)
        for record in pending:
            evidence_payload = row_evidence_payload(
                retention_id=target_id,
                content_digest=content_digest,
                line_no=record["line_no"],
                accepted=bool(record["accepted"]),
                reason_code=record["reason_code"],
                raw_digest=record["raw_digest"],
            )
            store.insert_evidence(
                db, target_id, record["line_no"], record["accepted"],
                record["reason_code"], record["raw_digest"],
                sign(evidence_payload), stamp,
            )
            # 先落核验依据，再删原文，最后推进检查点；同一事务内顺序提交。
            store.delete_record(db, record["id"])
            processed += 1
            last_line = record["line_no"]
            store.touch_progress(db, target_id, last_line, processed, stamp)
            db.commit()

        assert store.remaining_record_count(db, target_id) == 0
        store.mark_status(db, target_id, "DESTROYED")
        db.commit()
        evidence = store.list_evidence(db, target_id)
        final_row = store.get_retention(db, target_id)
        receipt = destruction_receipt(
            credential=_credential_from_row(final_row),
            destroyed_at=stamp,
            evidence_count=len(evidence),
            resumed=resumed,
            status="DESTROYED",
        )

    return {
        "outcome": "DESTROYED",
        "retention_id": target_id,
        "status": "DESTROYED",
        "resumed": resumed,
        "destroyed_records": processed,
        "evidence_count": len(evidence),
        "early": not due,
        "receipt": receipt,
    }


def verify_original(*, request_id: str, retention_id: str,
                    manifest_text: str) -> dict:
    """核验时由责任方提交原件，与留存的不可还原依据逐行验真。

    - 凭据签名先验真，再逐行比对 SHA-256；
    - 已销毁：原件只参与本次比对，不落库、不留存；
    - 仍在留存：同样按摘要比对，原件不另存副本。
    """
    payload = manifest_text.encode("utf-8")
    submitted_digest = sha256_bytes(payload)

    store.init_db()
    with store.connect() as db:
        ledger = store.find_ledger(db, request_id)
        if ledger is None:
            raise DomainError("UNKNOWN_REQUEST", "请求 ID 未登记，无法核验", status=404)
        if ledger["retention_id"] != retention_id:
            raise DomainError(
                "REQUEST_RETENTION_MISMATCH", "请求 ID 与留存记录不匹配", status=409
            )
        row = store.get_retention(db, retention_id)
        if row is None:
            raise DomainError("UNKNOWN_RETENTION", "留存记录不存在", status=404)

        credential = _credential_from_row(row)
        credential_ok = verify(credential["payload"], credential["signature"])
        content_match = submitted_digest == row["content_digest"]

        accepted_rows, quarantined_rows, _ = parse_real_manifest(manifest_text)
        submitted = {item["line_no"]: item for item in accepted_rows}
        submitted.update({item["line_no"]: item for item in quarantined_rows})

        evidence = store.list_evidence(db, retention_id)
        basis = {item["line_no"]: item for item in evidence}
        line_results = []
        for line_no in sorted(set(submitted) | set(basis)):
            submitted_line = submitted.get(line_no)
            basis_line = basis.get(line_no)
            line_results.append({
                "line_no": line_no,
                "digest_match": bool(
                    submitted_line and basis_line
                    and submitted_line["raw_digest"] == basis_line["raw_digest"]
                ),
                "reason_code": basis_line["reason_code"] if basis_line else None,
                "basis_available": basis_line is not None,
            })
        rows_match = all(item["digest_match"] for item in line_results) and bool(line_results)

    return {
        "retention_id": retention_id,
        "status": row["status"],
        "credential_valid": credential_ok,
        "content_digest_match": content_match,
        "rows_match": rows_match,
        "verified": bool(credential_ok and content_match and rows_match),
        "line_results": line_results,
        "original_persisted": False,
    }


def read_status(retention_id: str) -> dict:
    """回读保留状态与原接收数量（销毁后数量仍可追溯，原文不可回读）。"""
    store.init_db()
    with store.connect() as db:
        row = store.get_retention(db, retention_id)
        if row is None:
            raise DomainError("UNKNOWN_RETENTION", "留存记录不存在", status=404)
        progress = store.get_progress(db, retention_id)
        evidence = store.list_evidence(db, retention_id)
        return {
            "retention_id": row["retention_id"],
            "status": row["status"],
            "request_id": row["request_id"],
            "content_digest": row["content_digest"],
            "received_count": row["received_count"],
            "accepted_count": row["accepted_count"],
            "quarantined_count": row["quarantined_count"],
            "received_at": row["received_at"],
            "retain_until": row["retain_until"],
            "destroyed_records": progress["processed"] if progress else 0,
            "evidence_count": len(evidence),
            "raw_text_available": row["status"] != "DESTROYED",
        }
