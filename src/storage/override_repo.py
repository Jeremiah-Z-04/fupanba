"""人工修正层：override 记录 + 日志

原则：人工修正永不覆盖算法结果（algo_rank 不可变），
只写 final_rank / is_override 与 override_log，
日志即"人工干预次数"指标 + 后续调参的标注数据。
"""
from __future__ import annotations

from datetime import date

from src.storage.db import DB


class OverrideRepo:
    def __init__(self, db: DB):
        self.db = db

    def rerank(self, trade_date: date, theme: str, code: str, new_rank: int) -> None:
        r = self.db.query(
            "SELECT final_rank FROM leader_result WHERE trade_date=? AND theme_name=? AND code=?",
            (trade_date.isoformat(), theme, code),
        )
        if not r:
            raise KeyError(f"标的不在结果中: {trade_date} {theme} {code}")
        old_rank = r[0]["final_rank"]
        self.db.execute(
            "UPDATE leader_result SET final_rank=?, is_override=1 "
            "WHERE trade_date=? AND theme_name=? AND code=?",
            (new_rank, trade_date.isoformat(), theme, code),
        )
        self.db.execute(
            "INSERT INTO override_log (trade_date, theme_name, code, action, old_rank, new_rank) "
            "VALUES (?,?,?,?,?,?)",
            (trade_date.isoformat(), theme, code, "rerank", old_rank, new_rank),
        )

    def move_out(self, trade_date: date, theme: str, code: str, reason: str = "") -> None:
        """软删除：人工把标的一出题材。保留行便于复原。"""
        self.db.execute(
            "UPDATE leader_result SET is_removed=1, is_override=1 "
            "WHERE trade_date=? AND theme_name=? AND code=?",
            (trade_date.isoformat(), theme, code),
        )
        self.db.execute(
            "INSERT INTO override_log (trade_date, theme_name, code, action, detail) "
            "VALUES (?,?,?,?,?)",
            (trade_date.isoformat(), theme, code, "move_out", reason),
        )

    def move_in(self, trade_date: date, from_theme: str, to_theme: str,
                code: str, name: str) -> None:
        self.move_out(trade_date, from_theme, code, f"move to {to_theme}")
        self.db.execute(
            """INSERT OR REPLACE INTO leader_result
               (trade_date, theme_name, code, name, algo_rank, final_rank,
                lianban_height, is_override, is_removed)
               VALUES (?,?,?,?,999,999,1,1,0)""",
            (trade_date.isoformat(), to_theme, code, name),
        )

    def add_to_theme(self, trade_date: date, theme: str, code: str, name: str = "",
                     lianban: int = 1) -> bool:
        """把未归纳的个股手动归纳进某个题材。

        与 move_in 的区别：move_in 是「从 A 挪到 B」，这里是从「无」到「有」，
        所以不能走 move_out（会写一条无意义的移出日志），而且连板数要按真实值写，
        不能像 move_in 那样写死 1。返回 False 表示本来就在该题材里。
        """
        ds = trade_date.isoformat()
        if not self.db.query("SELECT 1 FROM theme_result WHERE trade_date=? AND theme_name=?",
                             (ds, theme)):
            raise KeyError(f"题材不存在: {theme}")
        cur = self.db.query(
            "SELECT is_removed FROM leader_result WHERE trade_date=? AND theme_name=? AND code=?",
            (ds, theme, code))
        if cur and not cur[0]["is_removed"]:
            return False
        if cur:                                   # 曾被移出，恢复即可
            self.db.execute(
                "UPDATE leader_result SET is_removed=0, is_override=1 "
                "WHERE trade_date=? AND theme_name=? AND code=?",
                (ds, theme, code))
        else:
            self.db.execute(
                "INSERT OR REPLACE INTO leader_result "
                "(trade_date, theme_name, code, name, algo_rank, final_rank, "
                " lianban_height, is_override, is_removed) "
                "VALUES (?,?,?,?,999,999,?,1,0)",
                (ds, theme, code, name, max(0, int(lianban or 0))))
        self.db.execute(
            "INSERT INTO override_log (trade_date, theme_name, code, action, detail) "
            "VALUES (?,?,?,?,?)",
            (ds, theme, code, "move_in", "手动归纳"))
        self.recount_theme(trade_date, theme)
        return True

    def recount_theme(self, trade_date: date, theme: str) -> None:
        """按 leader_result 实况重算题材家数。

        人工增删成员后必须同步，否则左栏家数、题材排序、导出文件名都对不上。
        """
        ds = trade_date.isoformat()
        row = self.db.query(
            "SELECT "
            " (SELECT COUNT(*) FROM leader_result l JOIN daily_zt_pool p "
            "   ON p.trade_date=l.trade_date AND p.code=l.code AND p.sealed=1 "
            "  WHERE l.trade_date=? AND l.theme_name=? AND l.is_removed=0) AS zt, "
            " (SELECT COUNT(*) FROM leader_result l "
            "  WHERE l.trade_date=? AND l.theme_name=? AND l.is_removed=0) AS total",
            (ds, theme, ds, theme))[0]
        total = row["total"] or 0
        zt = row["zt"] or 0
        self.db.execute(
            "UPDATE theme_result SET zt_count=?, strong_count=?, member_count=? "
            "WHERE trade_date=? AND theme_name=?",
            (zt, total - zt, total, ds, theme))

    def reset_theme(self, trade_date: date, theme: str) -> int:
        """复原单个题材：删掉人工新增的行，其余重置为算法排名。返回影响行数。"""
        ds = trade_date.isoformat()
        # add_to_theme 用 algo_rank=999 做标记（算法排名永远 < 999），复原时要清掉
        added = self.db.execute(
            "DELETE FROM leader_result WHERE trade_date=? AND theme_name=? "
            "AND is_override=1 AND algo_rank>=999", (ds, theme)).rowcount
        cur = self.db.execute(
            "UPDATE leader_result SET final_rank=algo_rank, is_override=0, is_removed=0 "
            "WHERE trade_date=? AND theme_name=? AND (is_override=1 OR is_removed=1)",
            (ds, theme),
        )
        n = cur.rowcount + added
        self.recount_theme(trade_date, theme)
        self.db.execute(
            "INSERT INTO override_log (trade_date, theme_name, code, action, detail) "
            "VALUES (?,?,?,?,?)",
            (ds, theme, "", "reset_theme", f"复原 {n} 处"),
        )
        return n

    def reset_day(self, trade_date: date) -> int:
        """复原整个交易日：删掉人工新增的行，其余恢复纯算法结果。返回影响行数。"""
        ds = trade_date.isoformat()
        added = self.db.execute(
            "DELETE FROM leader_result WHERE trade_date=? "
            "AND is_override=1 AND algo_rank>=999", (ds,)).rowcount
        cur = self.db.execute(
            "UPDATE leader_result SET final_rank=algo_rank, is_override=0, is_removed=0 "
            "WHERE trade_date=? AND (is_override=1 OR is_removed=1)",
            (ds,),
        )
        n = cur.rowcount + added
        for row in self.db.query(
                "SELECT theme_name FROM theme_result WHERE trade_date=?", (ds,)):
            self.recount_theme(trade_date, row["theme_name"])
        self.db.execute(
            "INSERT INTO override_log (trade_date, theme_name, code, action, detail) "
            "VALUES (?,?,?,?,?)",
            (ds, "", "", "reset_day", f"复原 {n} 处"),
        )
        return n

    def daily_intervention_count(self, trade_date: date) -> int:
        """人工干预次数指标（PRD 核心指标之一）"""
        r = self.db.query(
            "SELECT COUNT(*) c FROM override_log WHERE trade_date=?",
            (trade_date.isoformat(),),
        )
        return r[0]["c"] if r else 0
