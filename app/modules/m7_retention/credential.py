"""验收凭据的签发与回读组装。

凭据载荷全部为不可还原信息（指纹、计数、期限、状态），并用 HMAC 签名。
销毁后原文消失，凭据仍可独立证明“何时接收了什么、隔离结果如何、
何时由谁完成销毁”，核验时再由提交者出示原件与留存摘要比对。
"""
from .crypto import sign


def issue_credential(*, retention_id: str, content_digest: str, request_id: str,
                     received_count: int, accepted_count: int, quarantined_count: int,
                     received_at: str, retain_until: str) -> dict:
    payload = {
        "retention_id": retention_id,
        "content_digest": content_digest,
        "request_id": request_id,
        "received_count": received_count,
        "accepted_count": accepted_count,
        "quarantined_count": quarantined_count,
        "received_at": received_at,
        "retain_until": retain_until,
        "credential_type": "RETENTION_ACCEPTANCE",
    }
    return {"payload": payload, "signature": sign(payload), "algorithm": "HMAC-SHA256"}


def destruction_receipt(*, credential: dict, destroyed_at: str, evidence_count: int,
                        resumed: bool, status: str) -> dict:
    """销毁回执复用原验收凭据，只追加销毁事实并重新签名。"""
    payload = {
        "credential": credential,
        "destroyed_at": destroyed_at,
        "evidence_count": evidence_count,
        "resumed": resumed,
        "status": status,
        "credential_type": "DESTRUCTION_RECEIPT",
    }
    return {"payload": payload, "signature": sign(payload), "algorithm": "HMAC-SHA256"}


def row_evidence_payload(*, retention_id: str, content_digest: str, line_no: int,
                         accepted: bool, reason_code: str | None, raw_digest: str) -> dict:
    return {
        "retention_id": retention_id,
        "content_digest": content_digest,
        "line_no": line_no,
        "accepted": accepted,
        "reason_code": reason_code,
        "raw_digest": raw_digest,
        "credential_type": "ROW_VERIFICATION_BASIS",
    }
