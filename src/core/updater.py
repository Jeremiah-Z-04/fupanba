"""应用内自动更新（仅打包版 exe 生效）。

流程：
  1. 读 config.update.manifest_url（固定地址的 manifest JSON）
  2. 比较 version.json 与 manifest.version
  3. 下载 zip → 校验 sha256 → 解压到 data/_update/new
  4. 生成 data/_update/apply.bat 并启动，随后主进程退出
  5. bat 等 2 秒 → taskkill 旧进程 → robocopy 覆盖 _internal → 换 exe →
     写新 version.json → 删临时文件 → 重新启动 exe

保留：data/（数据库/导出/日志）、.env、config/（用户改过的配置）
替换：复盘吧.exe、_internal/、version.json

安全：
  - manifest 必须 https（本机调试允许 http://127.0.0.1 / localhost）
  - zip 必须匹配 manifest.sha256
  - zip 内必须同时有 复盘吧.exe 和 _internal/，否则拒绝执行
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from src.log import get_logger
from src.paths import APP_DIR, is_frozen

log = get_logger(__name__)

VERSION_FILE = "version.json"
MANAGED_EXE = Path(sys.executable).name if is_frozen() else "复盘吧.exe"


# ------------------------------------------------ 版本
def current_version() -> str:
    p = APP_DIR / VERSION_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return str(data.get("version") or "")
    except Exception:
        return "dev" if not is_frozen() else ""


def write_version(app_dir: Path, version: str | None = None) -> str:
    version = version or datetime.now().strftime("%Y%m%d.%H%M%S")
    payload = {
        "version": version,
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    (app_dir / VERSION_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return version


def _newer(latest: str, current: str) -> bool:
    if not latest:
        return False
    if not current or current == "dev":
        return False  # 源码运行不提示更新

    def parts(v: str) -> tuple:
        out = []
        for x in v.replace("-", ".").split("."):
            out.append(int(x) if x.isdigit() else 0)
        return tuple(out)

    return parts(latest) > parts(current)


# ------------------------------------------------ 检查
def check_for_update(cfg) -> dict:
    url = str(cfg.get("update.manifest_url", "") or "").strip()
    cur = current_version()
    base = {"enabled": False, "current": cur, "latest": "", "has_update": False}
    if not cfg.get("update.enabled", True):
        return {**base, "message": "自动更新已关闭"}
    if not url:
        return {**base, "message": "未配置更新地址"}
    if not (url.startswith("https://") or url.startswith(("http://127.0.0.1", "http://localhost"))):
        return {**base, "enabled": True, "message": "更新地址必须是 https"}

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ThemeHunter-Updater"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.warning("更新检查失败: %s", e)
        return {**base, "enabled": True, "message": f"检查更新失败：{e}"}

    latest = str(data.get("version") or "")
    return {
        "enabled": True,
        "current": cur,
        "latest": latest,
        "has_update": _newer(latest, cur),
        "notes": data.get("notes", ""),
        "size": data.get("size", 0),
        "published_at": data.get("published_at", ""),
        "message": "" if latest else "manifest 缺少 version",
    }


# ------------------------------------------------ 下载 / 解压
def _download(url: str, dst: Path, sha256: str, expected_size: int = 0) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    got = 0
    req = urllib.request.Request(url, headers={"User-Agent": "ThemeHunter-Updater"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            got += len(chunk)
            if got > 300 * 1024 * 1024:
                raise RuntimeError("更新包超过 300MB，已中止")
            h.update(chunk)
            f.write(chunk)
    if expected_size and got != int(expected_size):
        raise RuntimeError(f"更新包大小不符：{got} != {expected_size}")
    if sha256 and h.hexdigest().lower() != sha256.lower():
        raise RuntimeError("更新包 sha256 校验失败")


def _safe_extract(zip_path: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    base = dst.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = (dst / info.filename).resolve()
            if not str(target).startswith(str(base)):
                raise RuntimeError(f"更新包含非法路径: {info.filename}")
        z.extractall(dst)


def _validate_staged(new_dir: Path) -> None:
    if not (new_dir / MANAGED_EXE).exists():
        raise RuntimeError("更新包缺少 复盘吧.exe")
    if not (new_dir / "_internal").is_dir():
        raise RuntimeError("更新包缺少 _internal/")


# ------------------------------------------------ 应用
def _write_apply_script(base: Path) -> Path:
    """生成纯 ASCII 的替换脚本；中文路径通过环境变量传入，避开 cmd 代码页问题。"""
    bat = base / "apply.bat"
    content = r"""@echo off
setlocal
set "APP=%TH_APP%"
set "NEW=%TH_NEW%"
set "EXE=%TH_EXE%"
for %%F in ("%EXE%") do set "EXE_NAME=%%~nxF"
set "LOG=%~dp0apply.log"
ping -n 3 127.0.0.1 >nul
taskkill /IM "%EXE_NAME%" /F >nul 2>&1
ping -n 2 127.0.0.1 >nul
robocopy "%NEW%\_internal" "%APP%\_internal" /MIR /R:2 /W:1 /NFL /NDL /NJH /NJS >>"%LOG%" 2>&1
copy /Y "%NEW%\%EXE_NAME%" "%APP%\%EXE_NAME%" >>"%LOG%" 2>&1
copy /Y "%NEW%\version.json" "%APP%\version.json" >>"%LOG%" 2>&1
rd /S /Q "%NEW%" >nul 2>&1
del "%~dp0pkg.zip" >nul 2>&1
set "TH_NO_BROWSER=1"
start "" /D "%APP%" "%EXE%"
(goto) 2>nul & del "%~f0"
"""
    bat.write_text(content, encoding="ascii", errors="replace")
    return bat


def apply_update(cfg) -> dict:
    if not is_frozen():
        return {"ok": False, "message": "源码运行模式不支持自动更新（仅打包版可用）"}
    info = check_for_update(cfg)
    if not info.get("has_update"):
        return {"ok": False, "message": info.get("message") or "已是最新版本"}

    manifest_url = str(cfg.get("update.manifest_url", "")).strip()
    try:
        req = urllib.request.Request(manifest_url, headers={"User-Agent": "ThemeHunter-Updater"})
        with urllib.request.urlopen(req, timeout=10) as r:
            manifest = json.loads(r.read().decode("utf-8"))
        zip_url = urllib.parse.urljoin(manifest_url, str(manifest.get("url") or ""))
        sha = str(manifest.get("sha256") or "")
        if not zip_url.startswith("https://") and "127.0.0.1" not in zip_url:
            raise RuntimeError("更新包地址必须是 https")

        work = APP_DIR / "data" / "_update"
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        pkg = work / "pkg.zip"
        log.info("下载更新包: %s", zip_url)
        _download(zip_url, pkg, sha, int(manifest.get("size") or 0))

        new_dir = work / "new"
        _safe_extract(pkg, new_dir)
        _validate_staged(new_dir)

        bat = _write_apply_script(work)
        env = os.environ.copy()
        env.update({
            "TH_APP": str(APP_DIR),
            "TH_NEW": str(new_dir),
            "TH_EXE": str(APP_DIR / MANAGED_EXE),
        })
        kwargs = dict(cwd=str(work), env=env, close_fds=True,
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL)
        if os.name == "nt":
            base_flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            breakaway = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB：脱离父进程的 Job，防止被连带结束
            try:
                subprocess.Popen(["cmd", "/c", str(bat)], creationflags=base_flags | breakaway, **kwargs)
            except OSError:
                # 某些环境不允许 breakaway，退回普通分离模式
                subprocess.Popen(["cmd", "/c", str(bat)], creationflags=base_flags, **kwargs)
        else:
            subprocess.Popen(["cmd", "/c", str(bat)], **kwargs)
        log.warning("更新包已就绪，退出主进程等待替换：%s -> %s",
                    info["current"], manifest.get("version"))
        return {"ok": True, "message": "更新中，程序将自动重启",
                "latest": manifest.get("version")}
    except Exception as e:
        log.error("应用更新失败: %s", e)
        return {"ok": False, "message": f"更新失败：{e}"}
