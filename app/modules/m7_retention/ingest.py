"""真实清单接入。

复用 M1 的校验口径（BOM、列数不齐、空值、重复表头），但额外保留每行的
物理原文与逐行 SHA-256：M7 销毁的是清单文本及其隔离原值，需要精确到行
的不可还原核验依据。不读取任何图像二进制。
"""
import csv
import io

from app.modules.m1_intake.parser import REQUIRED
from .crypto import sha256_text

TAIL_TRUNCATED = "TRUNCATED_TAIL"


def parse_real_manifest(text: str) -> tuple[list[dict], list[dict], bool]:
    """返回 (采纳行, 隔离行, 尾行是否完整)。

    每个元素包含 line_no（物理行号，从 1 起）、raw_text（该行原始文本）、
    raw_digest（逐行摘要）；隔离行另带 reason_code。
    """
    tail_complete = not text or text.endswith("\n")
    if not text:
        return [], [{
            "line_no": 0,
            "reason_code": "EMPTY_FILE",
            "raw_text": "",
            "raw_digest": sha256_text(""),
        }], tail_complete

    physical_lines = text.splitlines()
    reader = csv.reader(io.StringIO(text, newline=""))
    rows = list(reader)
    header = [name.lstrip("﻿").strip() for name in rows[0]] if rows else []
    if not header or any(name not in header for name in REQUIRED):
        bad = [{
            "line_no": 1,
            "reason_code": "MISSING_COLUMN",
            "raw_text": ",".join(header),
            "raw_digest": sha256_text(",".join(header)),
        }]
        return [], bad, tail_complete

    index = {name: pos for pos, name in enumerate(header)}
    accepted, quarantined = [], []
    for line_no, columns in enumerate(rows[1:], start=2):
        raw_text = physical_lines[line_no - 1] if line_no - 1 < len(physical_lines) else ""
        digest = sha256_text(raw_text)
        if columns and len(columns) == len(header) and columns == header:
            continue  # 重复表头不是数据行，与 M1 口径一致
        if len(columns) != len(header) or any(columns[index[k]] is None for k in REQUIRED):
            quarantined.append({"line_no": line_no, "reason_code": "COLUMN_COUNT",
                                "raw_text": raw_text, "raw_digest": digest})
            continue
        fields = {name: columns[pos] for name, pos in index.items()}
        if not fields["issue"].strip() or not fields["page"].strip():
            quarantined.append({"line_no": line_no, "reason_code": "EMPTY_VALUE",
                                "raw_text": raw_text, "raw_digest": digest})
            continue
        accepted.append({"line_no": line_no, "fields": fields,
                         "raw_text": raw_text, "raw_digest": digest})

    if not tail_complete:
        last_line = physical_lines[-1]
        quarantined.append({
            "line_no": len(rows),
            "reason_code": TAIL_TRUNCATED,
            "raw_text": last_line,
            "raw_digest": sha256_text(last_line),
        })
    return accepted, quarantined, tail_complete
