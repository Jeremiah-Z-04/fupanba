# -*- coding: utf-8 -*-
"""复盘笔记：日 / 题材 / 个股三级纯文本。

按交易日存（记录当时的判断，回看才能验证对错）。
页签里额外展示该题材/个股的近几条历史笔记做脉络参考，不用每天重复写。

私人内容，永不进静态站——`deploy/export_static.py` 的 build_day() 是
显式白名单，不往里加就流不出去；也绝不能做成 theme_result/leader_result
的新列（那两张表是 SELECT *）。
"""
from __future__ import annotations

from src.storage.db import DB

SCOPES = ("day", "theme", "stock")
MAX_LEN = 4000          # 单条笔记上限，防误贴长文把库撑爆


class NoteRepo:
    def __init__(self, db: DB):
        self.db = db

    # ------------------------------------------------------------ 读
    def bundle(self, trade_date: str) -> dict:
        """某日全部笔记：{day, themes:{name:content}, stocks:{code:content}}

        一次拿齐三层，切日期只多一个请求。
        """
        out: dict = {"day": "", "themes": {}, "stocks": {}}
        for r in self.db.query(
                "SELECT scope, ref_key, content FROM review_note "
                "WHERE trade_date=? AND content<>''", (trade_date,)):
            if r["scope"] == "day":
                out["day"] = r["content"]
            elif r["scope"] == "theme":
                out["themes"][r["ref_key"]] = r["content"]
            elif r["scope"] == "stock":
                out["stocks"][r["ref_key"]] = r["content"]
        return out

    def get(self, scope: str, trade_date: str, ref_key: str = "") -> str:
        rows = self.db.query(
            "SELECT content FROM review_note "
            "WHERE scope=? AND trade_date=? AND ref_key=?",
            (scope, trade_date, ref_key or ""))
        return rows[0]["content"] if rows else ""

    def ref_history(self, scope: str, ref_key: str, before: str,
                    limit: int = 8) -> list[dict]:
        """该题材/个股在 before 之前的近几条笔记（倒序），用于脉络参考。"""
        if scope not in ("theme", "stock") or not ref_key:
            return []
        return [dict(r) for r in self.db.query(
            "SELECT trade_date, content FROM review_note "
            "WHERE scope=? AND ref_key=? AND trade_date<? AND content<>'' "
            "ORDER BY trade_date DESC LIMIT ?",
            (scope, ref_key, before, int(limit)))]

    def count(self, trade_date: str) -> int:
        rows = self.db.query(
            "SELECT COUNT(*) AS c FROM review_note "
            "WHERE trade_date=? AND content<>''", (trade_date,))
        return int(rows[0]["c"]) if rows else 0

    def days_with_notes(self, limit: int = 400) -> list[str]:
        """写过笔记的日期（倒序），给前端做「有笔记」标记。"""
        return [r["trade_date"] for r in self.db.query(
            "SELECT DISTINCT trade_date FROM review_note WHERE content<>'' "
            "ORDER BY trade_date DESC LIMIT ?", (int(limit),))]

    # ------------------------------------------------------------ 写
    def save(self, scope: str, trade_date: str, ref_key: str, content: str) -> dict:
        """UPSERT；内容为空则删掉该行（不留空壳）。返回 {saved, deleted, len}"""
        if scope not in SCOPES:
            raise ValueError("scope 必须是 day / theme / stock")
        key = "" if scope == "day" else (ref_key or "").strip()
        if scope != "day" and not key:
            raise ValueError("%s 笔记必须指定题材名或股票代码" % scope)
        text = (content or "").strip()[:MAX_LEN]

        if not text:
            self.db.execute(
                "DELETE FROM review_note WHERE scope=? AND trade_date=? AND ref_key=?",
                (scope, trade_date, key))
            return {"saved": False, "deleted": True, "len": 0}

        self.db.execute(
            "INSERT INTO review_note (scope, trade_date, ref_key, content) "
            "VALUES (?,?,?,?) ON CONFLICT(scope, trade_date, ref_key) DO UPDATE SET "
            "  content=excluded.content, updated_at=datetime('now','localtime')",
            (scope, trade_date, key, text))
        return {"saved": True, "deleted": False, "len": len(text)}
