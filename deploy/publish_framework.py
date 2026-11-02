# -*- coding: utf-8 -*-
"""导出题材框架到 docs/framework.json 并推送到 GitHub。

朋友的程序从下面两个地址之一拉这份文件，实现「框架自动更新」：
    https://raw.githubusercontent.com/Jeremiah-Z-04/fupanba/main/docs/framework.json
    https://cdn.jsdelivr.net/gh/Jeremiah-Z-04/fupanba@main/docs/framework.json

用法：
    python deploy/publish_framework.py              # 导出 + 提交 + 推送
    python deploy/publish_framework.py --dry-run    # 只导出，不推送
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Config                            # noqa: E402
from src.core import framework_sync                      # noqa: E402
from src.storage.db import DB                            # noqa: E402


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：\n{(r.stderr or r.stdout).strip()}")
    return r


def main() -> int:
    dry = "--dry-run" in sys.argv

    cfg = Config.load()
    db = DB(cfg.db_path)
    data = framework_sync.export(db, cfg)

    out = ROOT / "docs" / "framework.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    themes = len({r["theme"] for r in data["rows"] if r["enabled"]})
    size = out.stat().st_size / 1024
    print(f"已导出 {data['count']} 条 / {themes} 个启用题材 -> {out} ({size:.0f} KB)")
    print(f"版本号：{data['version']}")

    if dry:
        print("\n--dry-run：只导出，没有提交。")
        return 0

    if git("rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        print("\n还没有 git 仓库，先跑 deploy/README.md 里的初始化步骤。")
        return 1

    git("add", "docs/framework.json")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("\n框架没有变化，不用推。")
        return 0

    msg = f"框架更新 {date.today():%Y-%m-%d}"
    git("commit", "-m", msg)
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    push = git("push", "-u", "origin", branch, check=False)
    if push.returncode != 0:
        # github.com 常被墙，api.github.com 可达；降级走 REST API，不需要用户开 VPN
        print("git push 失败（github.com 不可达），改用 GitHub API 推送…")
        r = subprocess.run(
            [sys.executable, str(ROOT / "deploy" / "push_via_api.py")], cwd=ROOT)
        return r.returncode

    print(f"\n已推送：{msg}")
    print("朋友的程序下次启动（或手动点「同步框架」）就会拿到这份新框架。")
    print("raw.githubusercontent 是即时的；jsdelivr 有几分钟 CDN 缓存。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
