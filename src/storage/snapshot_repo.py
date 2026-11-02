"""涨停池快照 + 连板状态 + 概念成分的读写"""
from __future__ import annotations

from datetime import date

from src.datasources.base import ConceptBlock, ZTStock
from src.storage.db import DB


class SnapshotRepo:
    def __init__(self, db: DB):
        self.db = db

    # ------------------------------------------------ 涨停池
    def save_zt_pool(self, stocks: list[ZTStock], sealed: bool = True,
                     source: str = "") -> None:
        rows = [
            (s.trade_date.isoformat(), s.code, s.name, s.zt_time, s.last_zt_time,
             s.fengdan_amt, s.amount, s.float_mv, s.turnover, s.zhaban_count,
             s.high_pct, s.fall_pct, s.industry, int(s.is_st), int(s.is_yizi),
             int(s.is_bj), int(sealed), s.reason, s.lianban_hint, source,
             s.anomaly_tag, s.anomaly_content)
            for s in stocks
        ]
        self.db.executemany(
            """INSERT OR REPLACE INTO daily_zt_pool
               (trade_date, code, name, zt_time, last_zt_time, fengdan_amt,
                amount, float_mv, turnover, zhaban_count, high_pct, fall_pct,
                industry, is_st, is_yizi, is_bj, sealed, reason, lianban_height, source,
                anomaly_tag, anomaly_content)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )

    def load_zt_pool(self, trade_date: date, sealed: bool = True) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM daily_zt_pool WHERE trade_date=? AND sealed=? "
            "ORDER BY zt_time",
            (trade_date.isoformat(), int(sealed)),
        )
        return [dict(r) for r in rows]

    def delete_zt_pool(self, trade_date: date, sealed: int | None = None) -> int:
        """删除某日池数据（重采前全量刷新，避免上次盘中快照的残留行）。"""
        if sealed is None:
            cur = self.db.execute(
                "DELETE FROM daily_zt_pool WHERE trade_date=?", (trade_date.isoformat(),)
            )
        else:
            cur = self.db.execute(
                "DELETE FROM daily_zt_pool WHERE trade_date=? AND sealed=?",
                (trade_date.isoformat(), int(sealed)),
            )
        return cur.rowcount or 0

    def has_date(self, trade_date: date) -> bool:
        r = self.db.query(
            "SELECT 1 FROM daily_zt_pool WHERE trade_date=? LIMIT 1",
            (trade_date.isoformat(),),
        )
        return bool(r)

    # ------------------------------------------------ 连板状态
    def load_lianban_state(self) -> dict[str, dict]:
        rows = self.db.query("SELECT * FROM lianban_state WHERE height > 0")
        return {r["code"]: dict(r) for r in rows}

    def save_lianban_state(self, state: list[dict]) -> None:
        """整体重建连板状态表（每日盘后全量刷新）"""
        self.db.execute("DELETE FROM lianban_state")
        self.db.executemany(
            """INSERT INTO lianban_state (code, name, height, last_zt_date, yizi_count)
               VALUES (?,?,?,?,?)""",
            [(s["code"], s["name"], s["height"], s["last_zt_date"], s["yizi_count"])
             for s in state],
        )

    def get_height(self, code: str, trade_date: date) -> int:
        """查某 code 在指定日收盘后的连板高度（含当日）"""
        r = self.db.query(
            "SELECT height FROM lianban_state WHERE code=? AND last_zt_date=?",
            (code, trade_date.isoformat()),
        )
        return r[0]["height"] if r else 0

    # ------------------------------------------------ 概念成分
    def rebuild_concepts(self, blocks: list[ConceptBlock]) -> None:
        self.db.execute("DELETE FROM concept_member")
        rows = [
            (b.name, code, b.source)
            for b in blocks for code in b.codes
        ]
        self.db.executemany(
            "INSERT OR REPLACE INTO concept_member (concept_name, code, source) "
            "VALUES (?,?,?)",
            rows,
        )
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES ('concept_updated_at', "
            "datetime('now','localtime'))"
        )

    def concept_updated_at(self) -> str:
        r = self.db.query("SELECT value FROM meta WHERE key='concept_updated_at'")
        return r[0]["value"] if r else ""

    def last_reason(self, code: str) -> dict | None:
        """该股最近一次有涨停原因的记录（供盘中原因未出时沿用）。

        同花顺的涨停原因常常盘中还没写出来，但股票已经连板了（青岛食品、
        地素时尚都出现过）。用上次的原因归类比瞎猜概念可靠得多。
        """
        rows = self.db.query(
            "SELECT trade_date, reason FROM daily_zt_pool "
            "WHERE code=? AND reason IS NOT NULL AND TRIM(reason)<>'' "
            "ORDER BY trade_date DESC LIMIT 1", (code,))
        return dict(rows[0]) if rows else None

    def concepts_of(self, code: str) -> list[str]:
        rows = self.db.query(
            "SELECT concept_name FROM concept_member WHERE code=?", (code,)
        )
        return [r["concept_name"] for r in rows]

    def members_of(self, concept_name: str) -> list[str]:
        rows = self.db.query(
            "SELECT code FROM concept_member WHERE concept_name=?",
            (concept_name,),
        )
        return [r["code"] for r in rows]

    def all_concepts(self) -> list[str]:
        rows = self.db.query(
            "SELECT DISTINCT concept_name FROM concept_member ORDER BY concept_name"
        )
        return [r["concept_name"] for r in rows]

    def concept_sizes(self) -> dict[str, int]:
        """概念名 -> 成分股数量（判断概念宽窄，用于挡掉 AI/国企改革这类大杂烩）。"""
        rows = self.db.query(
            "SELECT concept_name, COUNT(*) AS n FROM concept_member GROUP BY concept_name"
        )
        return {r["concept_name"]: r["n"] for r in rows}
