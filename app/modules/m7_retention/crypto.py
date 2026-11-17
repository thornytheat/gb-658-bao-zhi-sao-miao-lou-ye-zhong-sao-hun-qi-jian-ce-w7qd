"""验收凭据与核验依据的密码学原语。

只使用标准库：SHA-256 生成不可还原的内容指纹，HMAC-SHA256 为验收凭据
和行级核验依据签名。销毁原文后，仅凭摘要与签名仍可独立验真。
"""
import hashlib
import hmac
import json

from app.config import CREDENTIAL_SECRET


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(payload: dict) -> bytes:
    # sort_keys 保证字段顺序不影响签名结果，分隔符固定避免拼接歧义。
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(payload: dict, secret: str | None = None) -> str:
    key = (secret or CREDENTIAL_SECRET).encode("utf-8")
    return hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()


def verify(payload: dict, signature: str, secret: str | None = None) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(sign(payload, secret), signature)
