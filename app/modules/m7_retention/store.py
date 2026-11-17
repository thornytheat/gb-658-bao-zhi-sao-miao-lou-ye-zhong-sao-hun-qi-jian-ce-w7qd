"""M7 留存与销毁的持久化层。

并发模型：SQLite 单库 + 条件 UPDATE/INSERT，连接开启 busy_timeout。
- content_digest 唯一索引保证同内容只有一条留存记录，销毁与导入并发时
  旧清单无法重新落库；
- request_ledger 把请求 ID 与内容指纹绑定，同 ID 换内容直接冲突；
- destroy_progress 做行级检查点，销毁中断后按已完成进度恢复。
"""
import sqlite3
from datetime import datetime, timezone

from app.config import DATABASE_PATH
from app.modules.m7_retention.crypto import sha256_bytes


def connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS m7_retentions (
                retention_id TEXT PRIMARY KEY,
                content_digest TEXT NOT NULL UNIQUE,
                request_id TEXT NOT NULL,
                received_count INTEGER NOT NULL,
                accepted_count INTEGER NOT NULL,
                quarantined_count INTEGER NOT NULL,
                received_at TEXT NOT NULL,
                retain_until TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('ACTIVE','DESTROYING','DESTROYED'))
            );

            CREATE TABLE IF NOT EXISTS m7_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                retention_id TEXT NOT NULL REFERENCES m7_retentions(retention_id),
                line_no INTEGER NOT NULL,
                accepted INTEGER NOT NULL,
                reason_code TEXT,
                raw_text TEXT,
                raw_digest TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS m7_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                retention_id TEXT NOT NULL,
                line_no INTEGER NOT NULL,
                accepted INTEGER NOT NULL,
                reason_code TEXT,
                raw_digest TEXT NOT NULL,
                row_signature TEXT NOT NULL,
                destroyed_at TEXT NOT NULL,
                UNIQUE (retention_id, line_no)
            );

            CREATE TABLE IF NOT EXISTS m7_destroy_progress (
                retention_id TEXT PRIMARY KEY REFERENCES m7_retentions(retention_id),
                last_line_no INTEGER NOT NULL DEFAULT -1,
                processed INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS m7_request_ledger (
                request_id TEXT PRIMARY KEY,
                content_digest TEXT NOT NULL,
                retention_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class IdempotentConflict(Exception):
    """同一请求 ID 提交了不同内容。"""

    def __init__(self, existing_digest: str):
        self.existing_digest = existing_digest
        super().__init__(f"request id bound to content {existing_digest[:12]}")


def find_ledger(db: sqlite3.Connection, request_id: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM m7_request_ledger WHERE request_id = ?", (request_id,)
    ).fetchone()


def find_retention_by_digest(db: sqlite3.Connection, content_digest: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM m7_retentions WHERE content_digest = ?", (content_digest,)
    ).fetchone()


def get_retention(db: sqlite3.Connection, retention_id: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM m7_retentions WHERE retention_id = ?", (retention_id,)
    ).fetchone()


def insert_retention(
    db: sqlite3.Connection,
    retention_id: str,
    content_digest: str,
    request_id: str,
    received_count: int,
    accepted_count: int,
    quarantined_count: int,
    received_at: str,
    retain_until: str,
) -> bool:
    """条件插入：并发下只有一方能落库，返回是否为本次新插入。"""
    before = db.total_changes
    db.execute(
        """
        INSERT OR IGNORE INTO m7_retentions
            (retention_id, content_digest, request_id, received_count,
             accepted_count, quarantined_count, received_at, retain_until, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
        """,
        (retention_id, content_digest, request_id, received_count,
         accepted_count, quarantined_count, received_at, retain_until),
    )
    return db.total_changes > before


def bind_request(db: sqlite3.Connection, request_id: str, content_digest: str,
                 retention_id: str, created_at: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO m7_request_ledger VALUES (?, ?, ?, ?)",
        (request_id, content_digest, retention_id, created_at),
    )


def insert_record(db: sqlite3.Connection, retention_id: str, line_no: int,
                  accepted: bool, reason_code: str | None, raw_text: str,
                  raw_digest: str) -> None:
    db.execute(
        """
        INSERT INTO m7_records
            (retention_id, line_no, accepted, reason_code, raw_text, raw_digest)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (retention_id, line_no, int(accepted), reason_code, raw_text, raw_digest),
    )


def mark_status(db: sqlite3.Connection, retention_id: str, status: str) -> None:
    db.execute(
        "UPDATE m7_retentions SET status = ? WHERE retention_id = ?",
        (status, retention_id),
    )


def ensure_progress(db: sqlite3.Connection, retention_id: str, stamp: str) -> sqlite3.Row:
    db.execute(
        """
        INSERT OR IGNORE INTO m7_destroy_progress
            (retention_id, last_line_no, processed, started_at, updated_at)
        VALUES (?, -1, 0, ?, ?)
        """,
        (retention_id, stamp, stamp),
    )
    return db.execute(
        "SELECT * FROM m7_destroy_progress WHERE retention_id = ?", (retention_id,)
    ).fetchone()


def touch_progress(db: sqlite3.Connection, retention_id: str, last_line_no: int,
                   processed: int, stamp: str) -> None:
    db.execute(
        """
        UPDATE m7_destroy_progress
        SET last_line_no = ?, processed = ?, updated_at = ?
        WHERE retention_id = ?
        """,
        (last_line_no, processed, stamp, retention_id),
    )


def iter_pending_records(db: sqlite3.Connection, retention_id: str, after_line_no: int):
    """按行号稳定顺序取未销毁原文，检查点之后继续。"""
    return db.execute(
        """
        SELECT id, line_no, accepted, reason_code, raw_text, raw_digest
        FROM m7_records
        WHERE retention_id = ? AND line_no > ?
        ORDER BY line_no ASC
        """,
        (retention_id, after_line_no),
    ).fetchall()


def insert_evidence(db: sqlite3.Connection, retention_id: str, line_no: int,
                    accepted: int, reason_code: str | None, raw_digest: str,
                    row_signature: str, destroyed_at: str) -> None:
    db.execute(
        """
        INSERT OR IGNORE INTO m7_evidence
            (retention_id, line_no, accepted, reason_code, raw_digest,
             row_signature, destroyed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (retention_id, line_no, accepted, reason_code, raw_digest,
         row_signature, destroyed_at),
    )


def delete_record(db: sqlite3.Connection, record_id: int) -> None:
    db.execute("DELETE FROM m7_records WHERE id = ?", (record_id,))


def remaining_record_count(db: sqlite3.Connection, retention_id: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM m7_records WHERE retention_id = ?", (retention_id,)
    ).fetchone()[0]


def list_evidence(db: sqlite3.Connection, retention_id: str):
    return db.execute(
        "SELECT * FROM m7_evidence WHERE retention_id = ? ORDER BY line_no ASC",
        (retention_id,),
    ).fetchall()


def get_progress(db: sqlite3.Connection, retention_id: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM m7_destroy_progress WHERE retention_id = ?", (retention_id,)
    ).fetchone()


def list_retentions(db: sqlite3.Connection):
    return db.execute(
        "SELECT * FROM m7_retentions ORDER BY received_at DESC"
    ).fetchall()


def digest_of_bytes(payload: bytes) -> str:
    return sha256_bytes(payload)
