# -*- coding: utf-8 -*-
"""复盘笔记（日/题材/个股三级）测试。

重点不是「能不能存一行」，而是四个容易出错的边界：
  1. 空内容必须真的删行，不能留空壳（否则前端以为写过、历史里出现空白条）
  2. 三层互不串味：同一天 day 内容不能污染 themes/stocks
  3. 跨日隔离：同题材在不同日期的笔记必须分开
  4. history 只返回「更早」的，且不包含当天自己
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.storage.db import DB
from src.storage.note_repo import MAX_LEN, NoteRepo

D1 = "2026-10-29"
D2 = "2026-10-30"


class NoteRepoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        self.repo = NoteRepo(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    # ---------------------------------------------------------- 基本读写
    def test_day_note_roundtrip(self):
        self.repo.save("day", D1, "", "今天算力最强，机器人分歧")
        self.assertEqual(self.repo.get("day", D1), "今天算力最强，机器人分歧")
        b = self.repo.bundle(D1)
        self.assertEqual(b["day"], "今天算力最强，机器人分歧")
        self.assertEqual(b["themes"], {})
        self.assertEqual(b["stocks"], {})

    def test_theme_and_stock_notes(self):
        self.repo.save("theme", D1, "机器人", "分歧转一致？")
        self.repo.save("stock", D1, "300750", "明天看它能否三板")
        b = self.repo.bundle(D1)
        self.assertEqual(b["themes"]["机器人"], "分歧转一致？")
        self.assertEqual(b["stocks"]["300750"], "明天看它能否三板")
        self.assertEqual(b["day"], "")

    def test_three_layers_isolated(self):
        """同一天的三层内容互不覆盖。"""
        self.repo.save("day", D1, "", "本日")
        self.repo.save("theme", D1, "芯片", "题材")
        self.repo.save("stock", D1, "600000", "个股")
        b = self.repo.bundle(D1)
        self.assertEqual((b["day"], b["themes"]["芯片"], b["stocks"]["600000"]),
                         ("本日", "题材", "个股"))

    # ---------------------------------------------------------- 边界
    def test_empty_content_deletes_row(self):
        self.repo.save("day", D1, "", "有内容")
        self.assertTrue(self.repo.count(D1))
        r = self.repo.save("day", D1, "", "")
        self.assertTrue(r["deleted"])
        self.assertEqual(self.repo.get("day", D1), "")
        self.assertEqual(self.repo.count(D1), 0)      # 不留空壳

    def test_whitespace_only_also_deletes(self):
        self.repo.save("day", D1, "", "x")
        self.repo.save("day", D1, "", "   \n  ")
        self.assertEqual(self.repo.count(D1), 0)

    def test_same_day_overwrite(self):
        self.repo.save("day", D1, "", "第一版")
        self.repo.save("day", D1, "", "第二版")
        self.assertEqual(self.repo.get("day", D1), "第二版")
        self.assertEqual(self.repo.count(D1), 1)

    def test_cross_day_isolated(self):
        self.repo.save("theme", D1, "机器人", "昨天分歧")
        self.repo.save("theme", D2, "机器人", "今天一致")
        self.assertEqual(self.repo.get("theme", D1, "机器人"), "昨天分歧")
        self.assertEqual(self.repo.get("theme", D2, "机器人"), "今天一致")

    def test_scope_validation(self):
        with self.assertRaises(ValueError):
            self.repo.save("bogus", D1, "", "x")
        with self.assertRaises(ValueError):
            self.repo.save("theme", D1, "", "x")      # 题材必须给名字
        with self.assertRaises(ValueError):
            self.repo.save("stock", D1, "", "x")      # 个股必须给代码

    def test_long_content_truncated(self):
        r = self.repo.save("day", D1, "", "字" * (MAX_LEN + 500))
        self.assertEqual(r["len"], MAX_LEN)
        self.assertEqual(len(self.repo.get("day", D1)), MAX_LEN)

    # ---------------------------------------------------------- 历史脉络
    def test_history_excludes_today_and_newer(self):
        self.repo.save("stock", "2026-10-28", "300750", "更早")
        self.repo.save("stock", D1, "300750", "前一天")
        self.repo.save("stock", D2, "300750", "当天")
        hist = self.repo.ref_history("stock", "300750", D2)
        self.assertEqual([h["content"] for h in hist], ["前一天", "更早"])
        self.assertEqual([h["trade_date"] for h in hist], [D1, "2026-10-28"])

    def test_history_scoped_by_ref(self):
        self.repo.save("theme", D1, "机器人", "机器人的")
        self.repo.save("theme", D1, "芯片", "芯片的")
        hist = self.repo.ref_history("theme", "机器人", D2)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["content"], "机器人的")

    def test_history_empty_for_day_scope(self):
        self.repo.save("day", D1, "", "本日")
        self.assertEqual(self.repo.ref_history("day", "", D2), [])

    # ---------------------------------------------------------- 其他
    def test_days_with_notes(self):
        self.repo.save("day", D1, "", "a")
        self.repo.save("theme", D2, "机器人", "b")
        days = self.repo.days_with_notes()
        self.assertEqual(days, [D2, D1])            # 倒序

    def test_new_db_has_review_note_table(self):
        cols = {r["name"] for r in self.db.query("PRAGMA table_info(review_note)")}
        self.assertEqual(cols, {"scope", "trade_date", "ref_key", "content", "updated_at"})


if __name__ == "__main__":
    unittest.main()
