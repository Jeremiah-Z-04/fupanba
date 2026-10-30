"""同花顺导入通道：把题材成分股导出成 txt / 图片，落到固定目录并定位到资源管理器

为什么不直接操作同花顺：主链路不做 RPA，规避风控。同花顺侧操作由用户手动完成
（自选股页面 -> 添加股票 -> 上传 txt 或图片），这里只负责把文件准备到位。

两条通道的取舍：
  txt  一个文件装整个板块，同花顺「添加股票」上传即导入 —— 主力路径
  图片 同花顺图片识别只认 6 位数字、一次 10~20 只上限，按批出图 —— 兜底路径
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from datetime import date as _date
from datetime import timedelta
from pathlib import Path
from typing import Sequence

from src.config import Config
from src.datasources.base import normalize_code
from src.log import get_logger

log = get_logger(__name__)

_CODE_RE = re.compile(r"^\d{6}$")
_ILLEGAL = set('\\/:*?"<>|')
_CTRL = {chr(i) for i in range(32)}


# ------------------------------------------------------------
# 文件名
# ------------------------------------------------------------
def sanitize_filename(name: str, max_len: int = 24) -> str:
    """题材名 -> Windows 合法文件名。

    题材名可能带「/」（AI/算力）、引号、冒号；Windows 还不允许以空格或点结尾。
    超长时截断并追加 4 位内容哈希，保证不同题材不会撞成同一个名字。
    """
    raw = (name or "").strip()
    s = "".join("_" if (c in _ILLEGAL or c in _CTRL) else c for c in raw)
    s = s.strip(" .")
    if not s:
        return "未命名"
    if len(s) > max_len:
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:4]
        s = f"{s[: max_len - 5]}-{digest}"
    return s


def unique_names(names: Sequence[str]) -> dict[str, str]:
    """为一组题材名分配互不冲突的安全文件名（Windows 大小写不敏感）。"""
    out: dict[str, str] = {}
    used: set[str] = set()
    for n in names:
        base = sanitize_filename(n)
        cand, i = base, 2
        while cand.casefold() in used:
            cand = f"{base}-{i}"
            i += 1
        used.add(cand.casefold())
        out[n] = cand
    return out


# ------------------------------------------------------------
# 分批与内容
# ------------------------------------------------------------
def build_txt_text(codes: Sequence[str]) -> bytes:
    """纯 6 位代码 + CRLF + ASCII。

    不带 BOM、不带名称：纯数字在 UTF-8 与 GBK 下字节完全相同，
    这样无论同花顺按哪种编码读都不会乱码。
    """
    return "\r\n".join(codes).encode("ascii")


def _split_batches(codes: Sequence[str], chunk: int) -> list[list[str]]:
    if not codes:
        return []
    if chunk <= 0 or len(codes) <= chunk:
        return [list(codes)]
    return [list(codes[i:i + chunk]) for i in range(0, len(codes), chunk)]


def _plan(theme: str, codes: Sequence[str], date_iso: str, chunk: int, ext: str) -> list[dict]:
    safe = sanitize_filename(theme)
    batches = _split_batches(codes, chunk)
    total = len(batches)
    plan: list[dict] = []
    for i, batch in enumerate(batches):
        if total == 1:
            name = f"{safe}_{date_iso}.{ext}"
        else:
            name = f"{safe}_{date_iso}_{i + 1:02d}of{total:02d}.{ext}"
        plan.append({
            "name": name,
            "theme": theme,
            "kind": ext,
            "index": i + 1,
            "total": total,
            "count": len(batch),
            "codes": batch,
        })
    return plan


def plan_txt_batches(theme: str, codes: Sequence[str], date_iso: str, chunk: int = 0) -> list[dict]:
    """chunk<=0 表示不分卷（同花顺 txt 上传没有字数上限，200 字只是粘贴框的限制）。"""
    return _plan(theme, codes, date_iso, chunk, "txt")


def plan_image_batches(theme: str, codes: Sequence[str], date_iso: str, chunk: int = 12) -> list[dict]:
    return _plan(theme, codes, date_iso, max(1, chunk), "png")


# ------------------------------------------------------------
# 目录
# ------------------------------------------------------------
def export_root(cfg: Config) -> Path:
    rel = cfg.get("export.ths.dir", "exports/ths") or "exports/ths"
    return cfg.data_dir / rel


def fallback_root() -> Path:
    """主目录不可写时的回落位置（用户文档目录）。"""
    return Path.home() / "Documents" / "复盘吧导出"


def allowed_roots(cfg: Config) -> list[Path]:
    """允许写入/定位的根目录：正常导出目录 + 回落目录。"""
    return [export_root(cfg), fallback_root()]


def day_dir(cfg: Config, date_iso: str) -> Path:
    """当天导出目录；主目录不可写（如程序放在 Program Files）时回落到「文档」。"""
    root = export_root(cfg)
    try:
        d = root / date_iso
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError as e:
        log.warning("导出目录不可写（%s），改用文档目录", e)
    fallback = fallback_root() / date_iso
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def peek_day_dir(cfg: Config, date_iso: str) -> Path:
    """只看不建：返回已存在的当日目录，都没有则返回主路径（供提示文案用）。"""
    candidates = [export_root(cfg) / date_iso, fallback_root() / date_iso]
    for d in candidates:
        if d.is_dir():
            return d
    return candidates[0]


def write_txt_file(path: Path, codes: Sequence[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_txt_text(codes))
    return path


def delete_stale_txt(day: Path, keep: set[str]) -> int:
    """清掉上一次导出留下、这次不再生成的 txt。png 一律不动（避免删掉刚上传的图）。"""
    removed = 0
    for p in day.glob("*.txt"):
        if p.name in keep:
            continue
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def prune_old_dirs(root: Path, keep_days: int = 7) -> int:
    """按目录名（日期）清理超期导出，非日期目录不动。"""
    if keep_days <= 0 or not root.is_dir():
        return 0
    cutoff = _date.today() - timedelta(days=keep_days)
    removed = 0
    for p in root.iterdir():
        if not p.is_dir():
            continue
        try:
            d = _date.fromisoformat(p.name)
        except ValueError:
            continue
        if d < cutoff:
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
    return removed


def ensure_inside(path: Path | str, root: Path | str) -> Path:
    """路径必须落在 root 内，防目录穿越。"""
    p = Path(path).resolve()
    r = Path(root).resolve()
    if not p.is_relative_to(r):
        raise ValueError("路径越界")
    return p


def ensure_inside_any(path: Path | str, roots: Sequence[Path | str]) -> Path:
    """路径落在任一允许目录内即可（正常目录或回落目录）。"""
    for r in roots:
        try:
            return ensure_inside(path, r)
        except ValueError:
            continue
    raise ValueError("路径越界")


def reveal_in_explorer(path: Path) -> bool:
    """打开资源管理器：目录直接打开，文件则选中它。

    explorer 无论成败都返回 1，所以不能看退出码；中文路径必须整条命令传字符串，
    走 list2cmdline 会给 /select, 和路径分别加引号导致解析失败。
    """
    if sys.platform != "win32":
        return False
    try:
        p = Path(path).resolve()
        if p.is_dir():
            os.startfile(str(p))
            return True
        subprocess.Popen(
            f'explorer /select,"{p}"',
            shell=False,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError as e:
        log.warning("打开资源管理器失败: %s", e)
        return False


# ------------------------------------------------------------
# 数据
# ------------------------------------------------------------
def collect_codes(db, trade_date, theme: str | None = None) -> list[dict]:
    """取当日题材成分股（与 /api/clipboard 同一条 SQL，多返回题材名）。

    排序即页面顺序：题材按涨停家数、成员数降序，题材内按龙头排名。
    """
    day = trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date)
    sql = ("SELECT l.code AS code, l.theme_name AS theme FROM leader_result l "
           "JOIN theme_result t ON t.trade_date=l.trade_date "
           "  AND t.theme_name=l.theme_name "
           "WHERE l.trade_date=? AND l.is_removed=0 ")
    params: list = [day]
    if theme:
        sql += "AND l.theme_name=? "
        params.append(theme)
    sql += "ORDER BY t.member_count DESC, t.zt_count DESC, l.final_rank"
    rows = db.query(sql, tuple(params))

    out: list[dict] = []
    for r in rows:
        code = normalize_code(str(r["code"]))
        if not _CODE_RE.match(code):
            continue
        out.append({"code": code, "theme": r["theme"]})
    return out


def group_by_theme(rows: Sequence[dict]) -> list[tuple[str, list[str]]]:
    """按题材分组，保序，只在题材内去重。

    跨题材去重会让一股多簇的股票从后一个板块里凭空消失（当日实测有 30 只这种股票），
    所以「按题材导出」必须题材内去重；全局去重只用于合并总表。
    """
    order: list[str] = []
    bucket: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    for r in rows:
        th = r.get("theme") or ""
        if th not in bucket:
            bucket[th] = []
            seen[th] = set()
            order.append(th)
        code = r.get("code") or ""
        if code in seen[th]:
            continue
        seen[th].add(code)
        bucket[th].append(code)
    return [(th, bucket[th]) for th in order]


def filter_themes(
    groups: Sequence[tuple[str, list[str]]],
    min_members: int = 1,
    max_themes: int = 0,
) -> tuple[list[tuple[str, list[str]]], list[dict]]:
    """「全部题材」筛选：单只题材建板块没有意义，默认过滤掉。"""
    floor = max(1, min_members)
    kept: list[tuple[str, list[str]]] = []
    skipped: list[dict] = []
    for th, codes in groups:
        if len(codes) < floor:
            skipped.append({"theme": th, "count": len(codes)})
        else:
            kept.append((th, codes))
    if max_themes and len(kept) > max_themes:
        skipped.extend({"theme": th, "count": len(c)} for th, c in kept[max_themes:])
        kept = kept[:max_themes]
    return kept, skipped


def global_dedup(groups: Sequence[tuple[str, list[str]]]) -> list[str]:
    """合并总表用：跨题材去重，保序。"""
    seen: set[str] = set()
    out: list[str] = []
    for _, codes in groups:
        for c in codes:
            if c in seen:
                continue
            seen.add(c)
            out.append(c)
    return out
