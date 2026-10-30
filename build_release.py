# -*- coding: utf-8 -*-
"""一键打包给朋友用的发布包。

用法:
    python build_release.py                    # 默认品牌：复盘吧
    python build_release.py --name 复盘啦       # 换个品牌名（exe/文件夹/压缩包/页面标题）
    python build_release.py --no-build         # 跳过 exe 构建（只重新组装 + 打 zip）

产物:
    dist/<名字>/                    可直接运行的文件夹
    dist/<名字>_给朋友_YYYYMMDD.zip  发给朋友的压缩包（含 .env、历史数据、说明）

注意:
    1. 包里 .env 是明文的同花顺 API Key，发给别人前确认是否要换成对方自己的；
    2. 历史数据取 data/themehunter.db 的一致性快照（WAL 下用 sqlite backup）；
    3. 前端/后端改动后必须重新 build，静态资源是打进 exe 的 _internal 里的。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

from src.core.updater import write_version

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
INSTRUCTIONS = ROOT / "release" / "使用说明.txt"
SPEC = ROOT / "build_exe.spec"


def build_exe(name: str) -> None:
    print(f"[1/3] PyInstaller 构建中（{name}）…")
    env = os.environ.copy()
    env["TH_APP_NAME"] = name
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC)],
        cwd=ROOT,
        env=env,
        check=True,
    )


def latest_data_date() -> str:
    try:
        con = sqlite3.connect(ROOT / "data" / "themehunter.db")
        row = con.execute("SELECT MAX(trade_date) FROM theme_result").fetchone()
        con.close()
        return row[0] if row and row[0] else "-"
    except Exception:
        return "-"


def _patch_brand(app: Path, name: str) -> None:
    """把打包进去的页面标题/品牌名换成新名字（源码保持不动）。"""
    if name == "复盘吧":
        return
    html = app / "_internal" / "web" / "index.html"
    if html.exists():
        text = html.read_text(encoding="utf-8").replace("复盘吧", name)
        html.write_text(text, encoding="utf-8")


def assemble(name: str, keep_version: bool = False) -> Path:
    app = DIST / name
    print("[2/3] 组装配置 / 数据 / 说明…")
    (app / "config").mkdir(parents=True, exist_ok=True)
    (app / "data").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "config" / "default.yaml", app / "config" / "default.yaml")

    env = ROOT / ".env"
    if env.exists():
        shutil.copy2(env, app / ".env")
    else:
        print("  ! 未找到 .env，包里将没有 API Key")

    db_dst = app / "data" / "themehunter.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_dst) + suffix)
        if p.exists():
            p.unlink()
    src = sqlite3.connect(str(ROOT / "data" / "themehunter.db"))
    dst = sqlite3.connect(str(db_dst))
    src.backup(dst)
    dst.close()
    src.close()

    cal = ROOT / "data" / "trading_days.json"
    if cal.exists():
        shutil.copy2(cal, app / "data" / "trading_days.json")

    # 打进去的日志/导出是开发过程的残留，清掉
    for d in ("logs", "exports"):
        p = app / "data" / d
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)

    text = INSTRUCTIONS.read_text(encoding="utf-8")
    text = text.replace("{DATA_CUTOFF}", latest_data_date()).replace("复盘吧", name)
    (app / "使用说明.txt").write_text(text, encoding="utf-8")

    _patch_brand(app, name)

    if keep_version and (app / "version.json").exists():
        print("  版本号保持:", json.loads((app / "version.json").read_text(encoding="utf-8"))["version"])
    else:
        print(f"  版本号: {write_version(app)}")
    return app


def make_zip(name: str, zip_name: str | None = None) -> Path:
    print("[3/3] 打包 zip…")
    fname = zip_name or f"{name}_给朋友_{datetime.date.today():%Y%m%d}"
    if not fname.lower().endswith(".zip"):
        fname += ".zip"
    out = DIST / fname
    if out.exists():
        out.unlink()
    app = DIST / name
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(app.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(DIST))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-build", action="store_true", help="跳过 PyInstaller 构建")
    parser.add_argument("--keep-version", action="store_true", help="不重新生成版本号（与更新站点保持一致）")
    parser.add_argument("--name", default="复盘吧", help="品牌名（exe/文件夹/页面标题）")
    parser.add_argument("--zip-name", default=None, help="压缩包文件名（不含 .zip 也行），默认 <名字>_给朋友_日期")
    args = parser.parse_args()

    if not args.no_build:
        build_exe(args.name)
    app = assemble(args.name, keep_version=args.keep_version)
    out = make_zip(args.name, args.zip_name)
    size = out.stat().st_size / 1024 / 1024
    print(f"\n完成: {out}  ({size:.1f} MB)")
    print(f"程序: {app / (args.name + '.exe')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
