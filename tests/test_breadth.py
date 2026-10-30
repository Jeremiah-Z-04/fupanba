"""情绪宽度口径测试（纯函数，无网络）

三个数的边界最容易错：
  - 9.90 是「>=」还是「>」（用户口径是 9.90 以上，含 9.90）
  - 创/科/北是「>10」，10.00 整不算
  - 92 开头是北交所（新版代码段），必须算进 tech_10
"""
from __future__ import annotations

import unittest

from src.core import breadth


class BreadthTest(unittest.TestCase):
    def test_thresholds(self):
        snap = {
            "600000": {"price_change_ratio_pct": 9.89},   # 不够
            "600001": {"price_change_ratio_pct": 9.90},   # 含边界
            "600002": {"price_change_ratio_pct": 10.00},  # 主板涨停
            "300001": {"price_change_ratio_pct": 10.00},  # 创：不大于 10
            "300002": {"price_change_ratio_pct": 10.01},  # 创：算
            "688001": {"price_change_ratio_pct": 15.0},
            "920001": {"price_change_ratio_pct": 20.0},   # 北交所
        }
        b = breadth.compute(snap)
        self.assertEqual(b["total"], 7)
        self.assertEqual(b["up_99"], 6)     # 9.89 之外的全算（9.90 含边界）
        self.assertEqual(b["tech_10"], 3)   # 300002 / 688001 / 920001（10.00 不算）

    def test_bad_values_ignored(self):
        snap = {
            "600000": {"price_change_ratio_pct": None},
            "600001": {"price_change_ratio_pct": ""},
            "600002": {},                                  # 缺字段
        }
        b = breadth.compute(snap)
        self.assertEqual(b, {"up_99": 0, "tech_10": 0, "total": 3})

    def test_empty(self):
        self.assertEqual(breadth.compute({}), {"up_99": 0, "tech_10": 0, "total": 0})
        self.assertEqual(breadth.compute(None), {"up_99": 0, "tech_10": 0, "total": 0})


if __name__ == "__main__":
    unittest.main()
