# -*- coding: utf-8 -*-
"""爆量题材算法测试。

重点验证四个容易算错的地方：
  1. 基准必须是「前5个交易日中的最低量」，且 base_date 指向那天
  2. 停牌/新股（窗口内不足5根）必须被挡掉，否则复牌必"爆量"
  3. volume=0 的行不能当基准（否则 ratio 无穷大）
  4. 2/3 两套聚合与原始行必须同源（左栏家数 = 右栏明细行数）
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.core import volume_surge as vs
from src.storage.db import DB
from src.storage.volume_repo import VolumeRepo

# 6 个交易日：前 5 天做基准窗口，最后一天是"当日"
DAYS = ["2026-10-23", "2026-10-26", "2026-10-27",
        "2026-10-28", "2026-10-29", "2026-10-30"]
TODAY = "2026-10-30"


class FakeCal:
    """只提供 days() 的交易日历替身。"""

    def days(self):
        return [date.fromisoformat(d) for d in DAYS]


def _mkdb(tmpdir: str) -> DB:
    return DB(Path(tmpdir) / "t.db")


class PrevTradingDaysTest(unittest.TestCase):
    def test_skips_weekend(self):
        got = vs.prev_trading_days(FakeCal(), date(2026, 10, 30), 5)
        self.assertEqual(got, DAYS[:5])
        self.assertNotIn("2026-10-24", got)   # 周六
        self.assertNotIn("2026-10-25", got)   # 周日

    def test_not_enough_history(self):
        got = vs.prev_trading_days(FakeCal(), date(2026, 10, 26), 5)
        self.assertEqual(got, ["2026-10-23"])


class ScanRowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _mkdb(self.tmp.name)
        self.repo = VolumeRepo(self.db)
        # A: 前5日最低 100（10-23），今日 500 -> 5.0 倍
        # B: 前5日最低 1000（10-23），今日 2100 -> 2.1 倍
        # C: 前5日最低 1000，今日 1900 -> 1.9 倍（低于2，落库但不在2倍档展示）
        # D: 只有 3 天数据（停牌），今日 5000 -> 必须被窗口完整性挡掉
        # E: 前5日有一天 volume=0，今日 5000 -> 基准不能取 0
        rows = [
            ("600001", [100, 200, 150, 300, 250], 500),
            ("600002", [1000, 1200, 1100, 1300, 1250], 2100),
            ("600003", [1000, 1000, 1000, 1000, 1000], 1900),
            ("600005", [0, 800, 900, 700, 600], 5000),
        ]
        for code, hist, today_vol in rows:
            data = [(d, code, v, v * 10.0, 10.0, 10.0)
                    for d, v in zip(DAYS[:5], hist)]
            data.append((TODAY, code, today_vol, today_vol * 10.0, 11.0, 10.0))
            self.repo.save_day_batch(data)
        # D 只有 3 天
        self.repo.save_day_batch([
            ("2026-10-28", "600004", 900, 9000.0, 10.0, 10.0),
            ("2026-10-29", "600004", 950, 9500.0, 10.0, 10.0),
            (TODAY, "600004", 5000, 50000.0, 11.0, 10.0),
        ])

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _scan(self, min_ratio=1.5, require_full=True):
        return vs.scan_rows(self.db, TODAY, DAYS[:5], min_ratio, 5, require_full)

    def test_ratio_and_base_date(self):
        got = {r["code"]: r for r in self._scan()}
        self.assertAlmostEqual(got["600001"]["ratio"], 5.0, places=3)
        self.assertEqual(got["600001"]["base_volume"], 100)
        self.assertEqual(got["600001"]["base_date"], "2026-10-23")
        self.assertAlmostEqual(got["600002"]["ratio"], 2.1, places=3)

    def test_incomplete_window_excluded(self):
        codes = {r["code"] for r in self._scan()}
        self.assertNotIn("600004", codes)          # 只有 3 天，被挡
        self.assertNotIn("600005", codes)          # 有一天 volume=0，剩下 4 天，同样被挡

    def test_partial_window_allowed_when_disabled(self):
        codes = {r["code"] for r in self._scan(require_full=False)}
        self.assertIn("600004", codes)             # 关掉完整性检查才出现
        rows = {r["code"]: r for r in self._scan(require_full=False)}
        # 600004 只有 10-28 / 10-29 两天落在基准窗口内（10-30 是当日）
        self.assertEqual(rows["600004"]["bars"], 2)

    def test_zero_volume_not_used_as_base(self):
        # 600005 前5日有一天 0；关掉完整性检查后，基准应取剩下的最小值 600，不能是 0
        rows = {r["code"]: r for r in self._scan(require_full=False)}
        self.assertIn("600005", rows)
        self.assertEqual(rows["600005"]["base_volume"], 600)
        self.assertAlmostEqual(rows["600005"]["ratio"], 5000 / 600, places=3)

    def test_sorted_by_ratio_desc(self):
        ratios = [r["ratio"] for r in self._scan()]
        self.assertEqual(ratios, sorted(ratios, reverse=True))


class BuildPayloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _mkdb(self.tmp.name)
        self.repo = VolumeRepo(self.db)
        self.repo.replace_surge(TODAY, [
            # code,name,vol,base,base_date,avg5,bars,ratio,pct,suspect,reason,rdate,src
            ("600001", "甲", 500, 100, "2026-10-23", 200, 5, 5.0, 9.98, 0, "机器人+减速器", TODAY, "reason"),
            ("600002", "乙", 2100, 1000, "2026-10-23", 1180, 5, 2.1, 3.2, 0, "", "", "concept"),
            ("600003", "丙", 1900, 1000, "2026-10-23", 1000, 5, 1.9, 1.0, 0, "", "", ""),
            ("600009", "壬", 9999, 100, "2026-10-23", 200, 3, 99.0, 10.0, 1, "", "", ""),
        ])
        self.repo.replace_theme_map(TODAY, [
            ("机器人", "600001", "减速器"),
            ("机器人", "600002", ""),
            ("芯片", "600001", "半导体设备"),
            ("芯片", "600003", ""),
        ])

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_two_and_three_mult_aggregation(self):
        p = vs.build_payload(self.db, TODAY)
        self.assertTrue(p["ready"])
        th = {t["theme_name"]: t for t in p["themes"]}
        # 机器人：5.0 与 2.1 都 >=2，只有 5.0 >=3
        self.assertEqual(th["机器人"]["count_2"], 2)
        self.assertEqual(th["机器人"]["count_3"], 1)
        self.assertAlmostEqual(th["机器人"]["max_2"], 5.0, places=2)
        # 芯片：5.0 >=2，1.9 <2 —— 1.9 那只不该进 2 倍档
        self.assertEqual(th["芯片"]["count_2"], 1)

    def test_suspect_excluded_everywhere(self):
        p = vs.build_payload(self.db, TODAY)
        self.assertNotIn("600009", {s["code"] for s in p["stocks"]})
        self.assertEqual(p["totals"]["surge_2"], 2)      # 只有甲、乙
        self.assertEqual(p["totals"]["suspect"], 1)

    def test_left_count_equals_right_rows(self):
        """左栏家数必须等于右栏同板块明细行数（切档后仍然成立）。"""
        p = vs.build_payload(self.db, TODAY)
        for mult in p["mults"]:
            for t in p["themes"]:
                rows = [s for s in p["stocks"]
                        if s["theme"] == t["theme_name"] and s["ratio"] >= mult]
                self.assertEqual(len(rows), t["count_%d" % mult],
                                 "%s 在 %d 倍档下家数与明细不一致" % (t["theme_name"], mult))

    def test_unmapped_counted(self):
        p = vs.build_payload(self.db, TODAY)
        # 乙(2.1) 有归属；没有未归类
        self.assertEqual(p["totals"]["unmapped_2"], 0)

    def test_empty_day(self):
        p = vs.build_payload(self.db, "2026-10-29")
        self.assertFalse(p["ready"])
        self.assertEqual(p["themes"], [])


if __name__ == "__main__":
    unittest.main()
