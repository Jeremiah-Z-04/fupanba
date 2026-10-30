# -*- coding: utf-8 -*-
"""题材框架同步：从固定地址拉最新框架，整表覆盖本地 kpl_theme。

为什么需要单独一条通道：
  应用更新包会**刻意保留** data/ 和 config/（不覆盖用户的数据库与配置），
  而题材框架就存在 data/themehunter.db 的 kpl_theme 表里。
  所以就算发了新 exe，朋友的框架也不会跟着变，必须单独同步。

约定：发布方（你）的框架是唯一真源，本地全量覆盖。
朋友自己手工加的词会被覆盖掉——这是"自动同步"的必然代价，取舍写在 README 里。
"""
from __future__ import annotations

import hashlib
import json
import urllib.request

from src.log import get_logger

log = get_logger(__name__)

# 主地址 raw.githubusercontent（更新即时）；备用 jsdelivr（国内 CDN，有几分钟缓存）
DEFAULT_URLS = (
    "https://raw.githubusercontent.com/Jeremiah-Z-04/fupanba/main/docs/framework.json",
    "https://cdn.jsdelivr.net/gh/Jeremiah-Z-04/fupanba@main/docs/framework.json",
)

TIMEOUT = 8
MIN_ROWS = 100          # 少于这个数视为异常响应，拒绝覆盖，避免把框架写成空表


def _get(url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": "ThemeHunter/framework-sync"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch(urls=None) -> dict | None:
    """按顺序尝试各个地址，全部失败返回 None（离线不该影响正常使用）。"""
    for u in (urls or DEFAULT_URLS):
        try:
            data = _get(u)
            if isinstance(data, dict) and isinstance(data.get("rows"), list):
                return data
            log.warning("框架同步：%s 返回格式不对", u)
        except Exception as e:
            log.info("框架同步：%s 失败（%s）", u, e)
    return None


def _local_version(db) -> str:
    try:
        rows = db.query("SELECT value FROM meta WHERE key='framework_version'")
        return rows[0]["value"] if rows else ""
    except Exception:
        return ""


def _set_version(db, version: str) -> None:
    if not version:
        return
    db.execute(
        "INSERT INTO meta (key, value) VALUES ('framework_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (version,))


def _set_meta(db, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def read_meta_json(db, key: str):
    """读同步过来的归并规则（theme_families / concept_theme_alias）。"""
    try:
        rows = db.query("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else None
    except Exception:
        return None


def apply(db, payload: dict) -> dict:
    """整表替换。DELETE + INSERT 放在同一事务里，中途失败会回滚。"""
    rows = payload.get("rows") or []
    if len(rows) < MIN_ROWS:
        raise ValueError(
            f"远程框架只有 {len(rows)} 条（低于 {MIN_ROWS}），拒绝覆盖")

    values = []
    for r in rows:
        theme = (r.get("theme") or "").strip()
        if not theme:
            continue
        values.append((
            theme,
            (r.get("sub") or "").strip(),
            r.get("source") or "kpl",
            1 if r.get("enabled", 1) else 0,
            r.get("note") or "",
            int(r.get("sort_order") or 0),
        ))
    if not values:
        raise ValueError("远程框架没有有效行")

    with db._lock:
        try:
            db.conn.execute("DELETE FROM kpl_theme")
            db.conn.executemany(
                "INSERT INTO kpl_theme "
                "(theme_name, sub_name, source, enabled, note, sort_order, updated_at) "
                "VALUES (?,?,?,?,?,?, datetime('now','localtime'))", values)
            db.conn.commit()
        except Exception:
            db.conn.rollback()
            raise

    _set_version(db, payload.get("version") or "")
    # 归并规则一起下发：theme_families 决定「百货零售→大消费」这类归并，
    # 只同步 kpl_theme 不同步它，朋友那边会多出几个孤儿板块
    for key, field in (("kpl_families", "families"), ("kpl_alias", "alias")):
        block = payload.get(field)
        if isinstance(block, (dict, list)) and block:
            _set_meta(db, key, json.dumps(block, ensure_ascii=False))

    themes = len({v[0] for v in values if v[3]})
    log.info("框架同步：已覆盖 %d 条 / %d 个启用题材", len(values), themes)
    return {"rows": len(values), "themes": themes,
            "version": payload.get("version") or ""}


def export(db, cfg=None) -> dict:
    """把本地框架导出成同步用的 JSON（发布脚本调用）。

    version 用内容哈希而不是时间戳：内容没变版本就不变，
    发布脚本才不会每天产生一个空提交，朋友端也能正确跳过。
    除题材树外还带上归并规则 —— 缺了它们朋友那边归类结果会和你不一样。
    """
    rows = db.query(
        "SELECT theme_name, sub_name, source, enabled, note, sort_order "
        "FROM kpl_theme ORDER BY theme_name, sort_order, sub_name")
    items = [{
        "theme": r["theme_name"], "sub": r["sub_name"],
        "source": r["source"], "enabled": r["enabled"],
        "note": r["note"] or "", "sort_order": r["sort_order"] or 0,
    } for r in rows]
    families = (cfg.get("clustering.theme_families", {}) or {}) if cfg else {}
    alias = (cfg.get("clustering.concept_theme_alias", {}) or {}) if cfg else {}

    body = {"rows": items, "families": families, "alias": alias}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    version = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return {"version": version, "count": len(items),
            "families": families, "alias": alias, "rows": items}


def sync(db, cfg=None, urls=None, force: bool = False) -> dict:
    """拉取并应用。供 API 和启动时后台线程共用。

    force=False 时先比版本：远程 version 和本地记录一致就不动数据库，
    避免把「本地刚改完还没发布」的框架冲掉。
    """
    if cfg is not None:
        custom = cfg.get("update.framework_url", "") or ""
        if custom:
            urls = (custom, *[u for u in DEFAULT_URLS if u != custom])
    payload = fetch(urls)
    if not payload:
        return {"ok": False, "reason": "拉取失败：网络不通或地址不可用"}

    remote_v = payload.get("version") or ""
    if not force and remote_v and remote_v == _local_version(db):
        return {"ok": True, "skipped": True, "reason": "已是最新", "version": remote_v}

    try:
        info = apply(db, payload)
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    return {"ok": True, "skipped": False, **info}
