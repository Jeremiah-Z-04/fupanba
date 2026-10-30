# -*- coding: utf-8 -*-
"""打一个能直接扔到云服务器上的部署包（本地运行）。

用法：
    python deploy/package.py            # 生成 release/fupanba_deploy_YYYYMMDD.tar.gz
    python deploy/package.py --no-db    # 不带历史数据（首次部署想从空库开始）

包里包含：源码、前端、配置、部署脚本、以及一份**一致性快照**的 SQLite 数据。
数据库用 sqlite3 的 backup API 复制，不直接拷 .db 文件——WAL 模式下直接拷贝
会漏掉还在 -wal 里没合并的数据。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import tarfile
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"__pycache__", ".git", ".venv", "build", "dist", "release", ".workbuddy"}
SKIP_SUFFIX = {".pyc", ".pyo", ".log", ".db-shm", ".db-wal"}


def copy_tree(src: Path, dst: Path) -> None:
    for p in src.rglob("*"):
        rel = p.relative_to(src)
        # 只按「路径里的目录部分」剪枝。不能拿文件名判下划线开头，
        # 否则 src/**/__init__.py 会被一起删掉，包导入直接崩。
        if any(part in SKIP_DIRS or part.startswith("_") for part in rel.parts[:-1]):
            continue
        if p.is_dir() and (p.name in SKIP_DIRS or p.name.startswith("_")):
            continue
        if p.suffix in SKIP_SUFFIX:
            continue
        target = dst / rel
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif p.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(p.read_bytes())


def snapshot_db(src: Path, dst: Path) -> None:
    """用 backup API 生成一致快照（等价于 sqlite3 的 .backup 命令）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")   # 服务器上单进程读写，用 DELETE 更省心


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-db", action="store_true", help="不打包历史数据库")
    ap.add_argument("--with-env", action="store_true", help="把 .env（同花顺 API Key）一起打进去")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="fupanba_"))
    pkg = tmp / "fupanba"
    pkg.mkdir()

    for name in ("src", "web", "config", "deploy"):
        src = ROOT / name
        if src.is_dir():
            copy_tree(src, pkg / name)
    for name in ("main.py", "start.py", "requirements-server.txt"):
        f = ROOT / name
        if f.is_file():
            (pkg / name).write_bytes(f.read_bytes())

    (pkg / "data").mkdir(exist_ok=True)
    if not args.no_db:
        db = ROOT / "data" / "themehunter.db"
        if db.is_file():
            snapshot_db(db, pkg / "data" / "themehunter.db")
            print(f"  数据快照 {(pkg / 'data' / 'themehunter.db').stat().st_size / 1e6:.1f} MB")
    cal = ROOT / "data" / "trading_days.json"
    if cal.is_file():
        (pkg / "data" / "trading_days.json").write_bytes(cal.read_bytes())

    if args.with_env:
        env = ROOT / ".env"
        if env.is_file():
            (pkg / ".env").write_bytes(env.read_bytes())
            print("  已包含 .env（里面有同花顺 API Key，别把包发给别人）")

    out_dir = ROOT / "release"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"fupanba_deploy_{date.today():%Y%m%d}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(pkg, arcname="fupanba")

    size = out.stat().st_size / 1e6
    print(f"\n打包完成：{out}  ({size:.1f} MB)")
    print("\n上传到服务器后执行：")
    print("  tar -xzf fupanba_deploy_*.tar.gz -C /opt")
    print("  sudo bash /opt/fupanba/deploy/setup.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
