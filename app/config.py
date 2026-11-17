import os
from pathlib import Path

APP_VERSION = os.getenv("APP_VERSION", "SF-2026.11")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/runs.sqlite3"))
RULE_VERSION = "scanfolio-rules-1"
TIMEZONE = "Asia/Shanghai"
# M7 验收凭据 HMAC 密钥；生产环境必须通过环境变量注入固定值，
# 否则进程重启后历史凭据将无法验真。
CREDENTIAL_SECRET = os.getenv("CREDENTIAL_SECRET", "scanfolio-dev-credential-secret")
DEFAULT_RETENTION_DAYS = int(os.getenv("DEFAULT_RETENTION_DAYS", "30"))

