# -*- coding: utf-8 -*-
"""题材框架（开盘啦层级表）的读写。

存储：daily_zt_pool 同库的 kpl_theme 表，一条 (一级题材, 细分词)。
来源分离是靠 source 字段：
  - kpl     框架导入的数据，重导时整批替换
  - manual  网页手工添加的数据，重导永不覆盖（这是"改文件"做不到的）
"""
from __future__ import annotations

from pathlib import Path

import yaml

from src.log import get_logger
from src.storage.db import DB

log = get_logger(__name__)


class ThemeRepo:
    def __init__(self, db: DB):
        self.db = db

    # ------------------------------------------------ 读取
    def tree(self, include_disabled: bool = False) -> dict[str, list[str]]:
        """一级题材 -> 细分词列表（供聚类加载）。"""
        sql = "SELECT theme_name, sub_name FROM kpl_theme"
        if not include_disabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY theme_name, sort_order, sub_name"
        out: dict[str, list[str]] = {}
        for r in self.db.query(sql):
            theme = r["theme_name"]
            out.setdefault(theme, [])
            if r["sub_name"]:
                out[theme].append(r["sub_name"])
        return out

    def list_themes(self, q: str = "") -> list[dict]:
        """题材列表（给网页表格用），按一级题材 + 细分词平铺。"""
        sql = ("SELECT theme_name, sub_name, source, enabled, note "
               "FROM kpl_theme")
        params: tuple = ()
        if q:
            sql += " WHERE theme_name LIKE ? OR sub_name LIKE ?"
            params = (f"%{q}%", f"%{q}%")
        sql += " ORDER BY theme_name, sort_order, sub_name"
        return [dict(r) for r in self.db.query(sql, params)]

    def stats(self) -> dict:
        r = self.db.query(
            "SELECT COUNT(*) AS total, "
            "COUNT(DISTINCT theme_name) AS themes, "
            "SUM(CASE WHEN source='manual' THEN 1 ELSE 0 END) AS manual, "
            "SUM(CASE WHEN enabled=0 THEN 1 ELSE 0 END) AS disabled "
            "FROM kpl_theme"
        )[0]
        return {k: (r[k] or 0) for k in ("total", "themes", "manual", "disabled")}

    # ------------------------------------------------ 写入
    def add(self, theme_name: str, sub_name: str = "", note: str = "") -> None:
        theme_name = (theme_name or "").strip()
        if not theme_name:
            raise ValueError("一级题材不能为空")
        self.db.execute(
            "INSERT INTO kpl_theme (theme_name, sub_name, source, enabled, note) "
            "VALUES (?,?, 'manual', 1, ?) "
            "ON CONFLICT(theme_name, sub_name) DO UPDATE SET "
            "  enabled=1, note=excluded.note, "
            "  updated_at=datetime('now','localtime')",
            (theme_name, (sub_name or "").strip(), note or ""),
        )

    def update(self, theme_name: str, sub_name: str, *,
               new_theme: str | None = None, new_sub: str | None = None,
               enabled: int | None = None, note: str | None = None) -> int:
        """改一条。改主键（改名）时先查旧行是否存在，避免误插。"""
        old = self.db.query(
            "SELECT * FROM kpl_theme WHERE theme_name=? AND sub_name=?",
            (theme_name, sub_name))
        if not old:
            return 0
        row = dict(old[0])
        theme = (new_theme if new_theme is not None else row["theme_name"]).strip()
        sub = (new_sub if new_sub is not None else row["sub_name"]).strip()
        if not theme:
            raise ValueError("一级题材不能为空")
        self.db.execute("DELETE FROM kpl_theme WHERE theme_name=? AND sub_name=?",
                        (theme_name, sub_name))
        self.db.execute(
            "INSERT INTO kpl_theme (theme_name, sub_name, source, enabled, note) "
            "VALUES (?,?,?,?,?)",
            (theme, sub, row["source"],
             1 if enabled is None else int(enabled),
             row["note"] if note is None else note),
        )
        return 1

    def delete(self, theme_name: str, sub_name: str) -> tuple[bool, str]:
        """删除。框架导入的数据只能停用，手工数据才能真删。"""
        rows = self.db.query(
            "SELECT source FROM kpl_theme WHERE theme_name=? AND sub_name=?",
            (theme_name, sub_name))
        if not rows:
            return False, "记录不存在"
        if rows[0]["source"] != "manual":
            return False, "框架导入的题材不能删除，请改为停用"
        self.db.execute("DELETE FROM kpl_theme WHERE theme_name=? AND sub_name=?",
                        (theme_name, sub_name))
        return True, "已删除"

    # ------------------------------------------------ 导入
    def seed_if_empty(self, yaml_path: Path) -> int:
        """库为空时用 yaml 做首次种子导入，返回导入条数。"""
        n = self.db.query("SELECT COUNT(*) AS c FROM kpl_theme")[0]["c"]
        if n:
            return 0
        return self.import_yaml(yaml_path)

    def import_yaml(self, yaml_path: Path) -> int:
        """整批重导框架：只清 source='kpl'，手工数据原样保留。"""
        if not yaml_path.exists():
            raise FileNotFoundError(f"框架文件不存在: {yaml_path}")
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        themes = data.get("themes") or {}
        if not isinstance(themes, dict) or not themes:
            raise ValueError("框架文件里没有 themes 数据")

        self.db.execute("DELETE FROM kpl_theme WHERE source='kpl'")
        rows = []
        order = 0
        for theme, subs in themes.items():
            theme = str(theme).strip()
            if not theme:
                continue
            rows.append((theme, "", "kpl", order))
            order += 1
            for sub in (subs or []):
                sub = str(sub).strip()
                if sub:
                    rows.append((theme, sub, "kpl", order))
        self.db.executemany(
            "INSERT OR IGNORE INTO kpl_theme "
            "(theme_name, sub_name, source, sort_order) VALUES (?,?,?,?)",
            rows,
        )
        log.info("题材框架导入完成: %d 个一级题材, %d 条记录（手工数据保留）",
                 len(themes), len(rows))
        return len(rows)

    # ------------------------------------------------ 导出
    def export_yaml(self, out_path: Path) -> int:
        """导出当前全量（含手工）为 yaml，供备份/分享。"""
        tree = self.tree(include_disabled=True)
        if not tree:
            raise ValueError("题材库为空，无需导出")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"themes": tree}, f, allow_unicode=True,
                           sort_keys=False, width=200)
        return sum(len(v) for v in tree.values())
