"""手动归纳到题材（override 层）测试

重点不是「能不能插一行」，而是三个容易出错的连带影响：
  1. 家数要按实况重算（涨停数只数 sealed=1，异动算 strong）
  2. 重复归纳不能产生重复行/重复日志
  3. 归纳后必须能被剪贴板和 TXT 导出取到（否则用户导出的少了股）
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.export import ths_files as ths
from src.storage.db import DB
from src.storage.override_repo import OverrideRepo

D1 = date(2026, 10, 28)


class AddToThemeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        self.ovr = OverrideRepo(self.db)
        ds = D1.isoformat()
        self.db.execute(
            "INSERT INTO theme_result (trade_date, theme_name, zt_count, member_count) "
            "VALUES (?,?,?,?)", (ds, "机器人", 1, 1))
        self.db.execute(
            "INSERT INTO leader_result (trade_date, theme_name, code, name, final_rank) "
            "VALUES (?,?,?,?,?)", (ds, "机器人", "600000", "浦发银行", 1))
        self.db.execute(
            "INSERT INTO daily_zt_pool (trade_date, code, name, sealed, reason) "
            "VALUES (?,?,?,?,?)", (ds, "600000", "浦发银行", 1, "银行"))
        # 池里两只：一只封板、一只创/科异动，都不在任何题材
        self.db.execute(
            "INSERT INTO daily_zt_pool (trade_date, code, name, sealed, reason, lianban_height) "
            "VALUES (?,?,?,?,?,?)", (ds, "300750", "宁德时代", 1, "锂电+储能", 3))
        self.db.execute(
            "INSERT INTO daily_zt_pool (trade_date, code, name, sealed, reason) "
            "VALUES (?,?,?,?,?)", (ds, "688315", "诺禾致源", 2, ""))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _theme(self):
        return self.db.query(
            "SELECT zt_count, strong_count, member_count FROM theme_result "
            "WHERE trade_date=? AND theme_name=?", (D1.isoformat(), "机器人"))[0]

    def test_add_recounts_and_keeps_lianban(self):
        added = self.ovr.add_to_theme(D1, "机器人", "300750", "宁德时代", 3)
        self.assertTrue(added)
        row = self.db.query(
            "SELECT lianban_height, is_override, is_removed, final_rank FROM leader_result "
            "WHERE trade_date=? AND theme_name=? AND code=?",
            (D1.isoformat(), "机器人", "300750"))[0]
        self.assertEqual(row["lianban_height"], 3)      # 不能像 move_in 那样写死 1
        self.assertEqual(row["is_override"], 1)
        self.assertEqual(row["is_removed"], 0)
        self.assertEqual(row["final_rank"], 999)        # 排在算法结果之后
        t = self._theme()
        self.assertEqual((t["zt_count"], t["strong_count"], t["member_count"]), (2, 0, 2))

    def test_strong_stock_counts_as_strong_not_zt(self):
        self.ovr.add_to_theme(D1, "机器人", "688315", "诺禾致源", 0)
        t = self._theme()
        self.assertEqual((t["zt_count"], t["strong_count"], t["member_count"]), (1, 1, 2))

    def test_add_twice_is_idempotent(self):
        self.assertTrue(self.ovr.add_to_theme(D1, "机器人", "300750", "宁德时代", 3))
        self.assertFalse(self.ovr.add_to_theme(D1, "机器人", "300750", "宁德时代", 3))
        n = self.db.query(
            "SELECT COUNT(*) c FROM override_log WHERE action='move_in'")[0]["c"]
        self.assertEqual(n, 1)
        self.assertEqual(self._theme()["member_count"], 2)

    def test_unknown_theme_raises(self):
        with self.assertRaises(KeyError):
            self.ovr.add_to_theme(D1, "不存在的题材", "300750", "x", 1)

    def test_reset_theme_removes_manual_add(self):
        self.ovr.add_to_theme(D1, "机器人", "300750", "宁德时代", 3)
        self.assertGreaterEqual(self.ovr.reset_theme(D1, "机器人"), 1)
        left = self.db.query(
            "SELECT COUNT(*) c FROM leader_result "
            "WHERE trade_date=? AND theme_name=? AND code=?",
            (D1.isoformat(), "机器人", "300750"))[0]["c"]
        self.assertEqual(left, 0)
        t = self._theme()
        self.assertEqual((t["zt_count"], t["member_count"]), (1, 1))

    def test_reset_day_removes_manual_add(self):
        self.ovr.add_to_theme(D1, "机器人", "688315", "诺禾致源", 0)
        self.assertGreaterEqual(self.ovr.reset_day(D1), 1)
        left = self.db.query(
            "SELECT COUNT(*) c FROM leader_result WHERE trade_date=? AND code=?",
            (D1.isoformat(), "688315"))[0]["c"]
        self.assertEqual(left, 0)
        t = self._theme()
        self.assertEqual((t["strong_count"], t["member_count"]), (0, 1))

    def test_added_stock_reaches_exports(self):
        """归纳完必须能被剪贴板 / TXT 导出取到，否则用户导出的少了股。"""
        self.ovr.add_to_theme(D1, "机器人", "300750", "宁德时代", 3)
        codes = [r["code"] for r in ths.collect_codes(self.db, D1, "机器人")]
        self.assertIn("300750", codes)
        # 与 /api/clipboard 完全同一条 SQL
        clip = [r["code"] for r in self.db.query(
            "SELECT l.code FROM leader_result l "
            "JOIN theme_result t ON t.trade_date=l.trade_date AND t.theme_name=l.theme_name "
            "WHERE l.trade_date=? AND l.is_removed=0 AND l.theme_name=? "
            "ORDER BY t.member_count DESC, t.zt_count DESC, l.final_rank",
            (D1.isoformat(), "机器人"))]
        self.assertIn("300750", clip)


if __name__ == "__main__":
    unittest.main()
