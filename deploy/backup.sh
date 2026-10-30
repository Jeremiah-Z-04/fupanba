#!/usr/bin/env bash
# 每日备份 SQLite。用 sqlite3 的 backup API 而不是 cp——
# 服务在跑的时候直接拷 .db 可能拿到写了一半的文件。
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/fupanba}"
KEEP_DAYS="${KEEP_DAYS:-14}"
DB="$APP_DIR/data/themehunter.db"
OUT_DIR="$APP_DIR/data/backups"

[ -f "$DB" ] || { echo "找不到数据库：$DB"; exit 1; }
mkdir -p "$OUT_DIR"

"$APP_DIR/.venv/bin/python" - "$DB" "$OUT_DIR" <<'PY'
import sqlite3, sys
from datetime import date
from pathlib import Path

db = Path(sys.argv[1])
out = Path(sys.argv[2]) / f"themehunter-{date.today():%Y%m%d}.db"
with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as s, sqlite3.connect(out) as d:
    s.backup(d)
print(f"已备份 -> {out} ({out.stat().st_size / 1e6:.1f} MB)")
PY

# 清理超期备份
find "$OUT_DIR" -name 'themehunter-*.db' -type f -mtime "+$KEEP_DAYS" -print -delete || true
