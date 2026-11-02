# -*- coding: utf-8 -*-
"""成交量 / 爆量结果的读写。

三张表：
  daily_volume        逐日全市场成交量（股）
  volume_surge        当日爆量股（一股一行，含倍数）
  volume_surge_theme  爆量股 × 题材板块（多对多）
"""
from __future__ import annotations

from src.storage.db import DB


class VolumeRepo:
    def __init__(self, db: DB):
        self.db = db

    # ------------------------------------------------------------ 逐日成交量
    def save_day(self, trade_date: str, rows, is_snapshot: int = 0,
                 source: str = "snapshot") -> int:
        """rows: [(code, volume, amount, close, prev_close)]，整日覆盖写。"""
        data = [(trade_date, str(c), float(v or 0), float(a or 0),
                 float(cl or 0), float(pc or 0), int(is_snapshot), source)
                for c, v, a, cl, pc in rows]
        if not data:
            return 0
        self.db.executemany(
            "INSERT OR REPLACE INTO daily_volume "
            "(trade_date, code, volume, amount, close, prev_close, is_snapshot, source) "
            "VALUES (?,?,?,?,?,?,?,?)", data)
        return len(data)

    def save_day_batch(self, rows) -> int:
        """跨日批量写（回补用）。rows: [(trade_date, code, volume, amount, close, prev_close)]"""
        if not rows:
            return 0
        self.db.executemany(
            "INSERT OR REPLACE INTO daily_volume "
            "(trade_date, code, volume, amount, close, prev_close, is_snapshot, source) "
            "VALUES (?,?,?,?,?,?,0,'kline')", rows)
        return len(rows)

    def coverage(self, dates) -> dict[str, int]:
        """各交易日的成交量行数，用于判断数据是否齐全。"""
        dates = [d for d in (dates or []) if d]
        if not dates:
            return {}
        ph = ",".join("?" * len(dates))
        rows = self.db.query(
            f"SELECT trade_date, COUNT(*) AS n FROM daily_volume "
            f"WHERE trade_date IN ({ph}) GROUP BY trade_date", tuple(dates))
        return {r["trade_date"]: r["n"] for r in rows}

    def codes_with_window(self, dates, need: int) -> set[str]:
        """窗口内数据齐全（>=need 天）的 code，用于回补断点续跑。"""
        dates = [d for d in (dates or []) if d]
        if not dates:
            return set()
        ph = ",".join("?" * len(dates))
        rows = self.db.query(
            f"SELECT code, COUNT(*) AS n FROM daily_volume "
            f"WHERE trade_date IN ({ph}) AND volume > 0 "
            f"GROUP BY code HAVING COUNT(*) >= ?", (*dates, int(need)))
        return {r["code"] for r in rows}

    def dates_with_data(self, limit: int = 60) -> list[str]:
        rows = self.db.query(
            "SELECT DISTINCT trade_date FROM daily_volume "
            "ORDER BY trade_date DESC LIMIT ?", (int(limit),))
        return [r["trade_date"] for r in rows]

    # ------------------------------------------------------------ 爆量结果
    def replace_surge(self, trade_date: str, rows) -> int:
        """整日替换。rows: [(trade_date 之外的全部字段...)]，见 _SURGE_COLS。"""
        self.db.execute("DELETE FROM volume_surge WHERE trade_date=?", (trade_date,))
        if not rows:
            return 0
        self.db.executemany(
            "INSERT OR REPLACE INTO volume_surge "
            "(trade_date, code, name, volume, base_volume, base_date, avg5_volume,"
            " bars, ratio, high_pct, is_suspect, reason, reason_date, map_source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(trade_date, *r) for r in rows])
        return len(rows)

    def replace_theme_map(self, trade_date: str, rows) -> int:
        """rows: [(theme_name, code, sub_tags)]"""
        self.db.execute("DELETE FROM volume_surge_theme WHERE trade_date=?", (trade_date,))
        if not rows:
            return 0
        self.db.executemany(
            "INSERT OR REPLACE INTO volume_surge_theme "
            "(trade_date, theme_name, code, sub_tags) VALUES (?,?,?,?)",
            [(trade_date, *r) for r in rows])
        return len(rows)

    def has_surge(self, trade_date: str) -> bool:
        return bool(self.db.query(
            "SELECT 1 FROM volume_surge WHERE trade_date=? LIMIT 1", (trade_date,)))

    def load_surge(self, trade_date: str, min_ratio: float = 2.0) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM volume_surge WHERE trade_date=? AND ratio>=? "
            "AND is_suspect=0 ORDER BY ratio DESC", (trade_date, float(min_ratio)))]

    def load_members(self, trade_date: str, min_ratio: float = 2.0) -> list[dict]:
        """板块成员明细（只取非 suspect）。"""
        return [dict(r) for r in self.db.query(
            "SELECT t.theme_name, t.sub_tags, s.code, s.name, s.volume, s.base_volume,"
            "       s.base_date, s.ratio, s.high_pct, s.bars, s.is_suspect,"
            "       s.reason, s.reason_date, s.map_source "
            "FROM volume_surge_theme t JOIN volume_surge s "
            "  ON s.trade_date=t.trade_date AND s.code=t.code "
            "WHERE t.trade_date=? AND s.ratio>=? AND s.is_suspect=0 "
            "ORDER BY t.theme_name, s.ratio DESC", (trade_date, float(min_ratio)))]

    def count_surge(self, trade_date: str) -> int:
        rows = self.db.query(
            "SELECT COUNT(*) AS c FROM volume_surge WHERE trade_date=?", (trade_date,))
        return int(rows[0]["c"]) if rows else 0
