# -*- coding: utf-8 -*-
"""导出最新复盘数据并推送到 GitHub，触发 Pages 自动更新。

用法：
    python deploy/publish_static.py                 # 导出全部历史 + 提交推送
    python deploy/publish_static.py --days 120      # 只保留最近 120 个交易日
    python deploy/publish_static.py --dry-run       # 只导出，不提交

前提：项目已 git init 并配好 origin。脚本会检查，缺了会告诉你下一步命令。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：\n{(r.stderr or r.stdout).strip()}")
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只导出最近 N 个交易日，0=全部")
    ap.add_argument("--dry-run", action="store_true", help="只导出，不提交")
    ap.add_argument("--message", default="", help="自定义提交信息")
    args = ap.parse_args()

    # ---------- 1. 检查仓库 ----------
    if git("rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        print("还没有 git 仓库。先做这两步：\n")
        print("  git init -b main")
        print("  git remote add origin https://github.com/你的用户名/fupanba.git\n")
        print("然后在 GitHub 上建一个仓库（Public，免费账号才能用 Pages），再来跑本脚本。")
        return 1

    if git("remote", "get-url", "origin", check=False).returncode != 0:
        print("还没有配置远程仓库：")
        print("  git remote add origin https://github.com/你的用户名/fupanba.git")
        return 1

    # 密钥绝不能进仓库
    tracked_env = git("ls-files", "--error-unmatch", ".env", check=False)
    if tracked_env.returncode == 0:
        print("危险：.env（含同花顺 API Key）已经被 git 跟踪了！")
        print("执行 `git rm --cached .env` 把它移出仓库，并确认 .gitignore 里有 .env")
        return 1

    # ---------- 2. 导出数据 ----------
    print("导出复盘数据…")
    cmd = [sys.executable, str(ROOT / "deploy" / "export_static.py")]
    if args.days:
        cmd += ["--days", str(args.days)]
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        return r.returncode

    if args.dry_run:
        print("\n--dry-run：数据已导出到 docs/，没有提交。")
        return 0

    # ---------- 3. 提交推送 ----------
    git("add", "docs")
    changed = git("diff", "--cached", "--quiet", check=False)
    if changed.returncode == 0:
        print("\ndocs/ 没有变化，不用推。")
        return 0

    msg = args.message or f"数据更新 {date.today():%Y-%m-%d}"
    git("commit", "-m", msg)
    print(f"\n已提交：{msg}")

    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    print(f"推送到 origin/{branch} …")
    push = git("push", "-u", "origin", branch, check=False)
    if push.returncode != 0:
        # github.com 常被墙/被代理拦，但 api.github.com 通常可达。
        # 降级走 REST API（复用凭据管理器里的 GitHub 记录），不强求用户开 VPN。
        print("git push 失败（github.com 不可达），改用 GitHub API 推送…")
        r = subprocess.run(
            [sys.executable, str(ROOT / "deploy" / "push_via_api.py")], cwd=ROOT)
        return r.returncode

    print("\n推送完成。")
    print("首次使用还需要在 GitHub 仓库里开一次 Pages：")
    print("  Settings → Pages → Source 选「Deploy from a branch」")
    print("  → Branch 选 main、目录选 /docs → Save")
    print("  约 1 分钟后访问 https://你的用户名.github.io/仓库名/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
