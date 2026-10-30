# -*- coding: utf-8 -*-
"""用 GitHub REST API 把 docs/ 一次性推上去。

为什么要绕开 git push：本机网络里 github.com 的 443 被墙/被代理拦截，
但 api.github.com 是通的。凭据复用 Windows 凭据管理器里的 GitHub 记录。

走 git data API（blobs → tree → commit → ref），一次提交推全部文件，
二进制文件（PNG 图标）也支持。
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OWNER, REPO, BRANCH = "Jeremiah-Z-04", "fupanba", "main"


def git_credential() -> tuple[str, str]:
    r = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    user = password = ""
    for line in r.stdout.splitlines():
        if line.startswith("username="):
            user = line.split("=", 1)[1]
        elif line.startswith("password="):
            password = line.split("=", 1)[1]
    if not password:
        raise SystemExit("凭据管理器里没有 GitHub 记录，请先在 Edge 里登录过一次 GitHub")
    return user, password


def api(method: str, path: str, payload=None):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": f"token {TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "fupanba-deploy",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"{method} {path} -> {e.code} {e.reason}\n{body}")


def collect() -> list[tuple[str, Path]]:
    """docs/ 全量 + 部署脚本 + 文档（代码也一起推，方便朋友 clone）。"""
    out: list[tuple[str, Path]] = []
    keep_dir = ("docs", "deploy", "src", "web", "config", "tests")
    # 注意：不能在 skip_dir 里写 "data" —— docs/data/ 是页面要读的复盘数据
    skip_dir = {"__pycache__", "_old", ".git", ".workbuddy", "build",
                "release", "backup", "dist"}
    skip_suffix = {".pyc", ".pyo", ".log", ".db-shm", ".db-wal"}
    for name in keep_dir:
        base = ROOT / name
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix in skip_suffix:
                continue
            if any(part in skip_dir or part.startswith("_") for part in p.relative_to(base).parts):
                continue
            out.append((str(p.relative_to(ROOT)).replace("\\", "/"), p))
    for name in ("main.py", "start.py", "requirements.txt", "requirements-server.txt",
                 "build_exe.spec", "build_release.py", ".gitignore", "daily_publish.bat",
                 "start_lan.bat", "start.bat"):
        p = ROOT / name
        if p.is_file():
            out.append((name, p))
    return out


def main() -> int:
    global TOKEN
    _, TOKEN = git_credential()

    user = api("GET", "/user")
    print(f"已认证：{user.get('login')}")

    files = collect()
    print(f"待推送：{len(files)} 个文件")
    total = sum(p.stat().st_size for p, _ in ((Path(f[0]), f[1]) for f in files))
    print(f"合计 {total/1024/1024:.1f} MB")

    # 空仓库不能直接建 blob（会报 Git Repository is empty），
    # 先用 contents API 落一个文件建立 main 分支
    try:
        api("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}")
    except RuntimeError:
        print("  仓库还是空的，先落第一个文件建立分支…")
        path, p0 = files.pop(0)
        api("PUT", f"/repos/{OWNER}/{REPO}/contents/{path}", {
            "message": "初始化仓库",
            "content": base64.b64encode(p0.read_bytes()).decode("ascii"),
            "branch": BRANCH,
        })
        print(f"  已创建 {path}")

    # 1. 逐个上传 blob（GitHub 的 create blob 只认 base64，不分文本二进制）
    tree_items = []
    for i, (path, p) in enumerate(files, 1):
        blob = api("POST", f"/repos/{OWNER}/{REPO}/git/blobs", {
            "content": base64.b64encode(p.read_bytes()).decode("ascii"),
            "encoding": "base64",
        })
        tree_items.append({
            "path": path,
            "mode": "100644",
            "type": "blob",
            "sha": blob["sha"],
        })
        if i % 20 == 0 or i == len(files):
            print(f"  已上传 {i}/{len(files)}")

    # 2. 提交到 main
    base = api("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}")
    parent = api("GET", f"/repos/{OWNER}/{REPO}/git/commits/{base['object']['sha']}")
    tree = api("POST", f"/repos/{OWNER}/{REPO}/git/trees",
               {"base_tree": parent["tree"]["sha"], "tree": tree_items})
    commit = api("POST", f"/repos/{OWNER}/{REPO}/git/commits", {
        "message": "导入复盘吧（含静态站 + 框架同步）",
        "tree": tree["sha"],
        "parents": [parent["sha"]],
    })
    api("PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}",
        {"sha": commit["sha"]})
    print(f"\n推送完成：{commit['sha'][:7]}  {len(files)} 个文件")
    print(f"https://github.com/{OWNER}/{REPO}")
    return 0


def _is_text(p: Path) -> bool:
    if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip",
                            ".gz", ".db", ".woff", ".woff2", ".ttf"}:
        return False
    try:
        p.read_bytes()[:2048].decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


if __name__ == "__main__":
    sys.exit(main())
