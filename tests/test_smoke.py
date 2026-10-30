"""端到端冒烟测试（离线，不依赖行情接口）

用合成的两日行情数据验证：
  1. 连板口径：晋级 / 断板清零 / ST 剔除 / 一字板计数 / 同日幂等
  2. 静态概念聚类 + 小簇剔除
  3. 题材归并（不做强度打分）
  4. 龙头排序：板幅归一优先（封板恒 1.0）、同强度看上板时间
  5. override 层：人工改排名不覆盖算法结果 + 干预次数统计
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.config import Config
from src.core.clustering.static_concept import StaticConceptClusterer
from src.core.leader_rank import LeaderRanker
from src.core.lianban import LianbanTracker
from src.datasources.base import ConceptBlock, ZTStock
from src.storage.db import DB
from src.storage.override_repo import OverrideRepo
from src.storage.snapshot_repo import SnapshotRepo

D1 = date(2026, 10, 21)
D2 = date(2026, 10, 22)


def make_stock(code, name, d, height_hint=0, **kw) -> ZTStock:
    return ZTStock(code=code, name=name, trade_date=d,
                   zt_time=kw.pop("zt_time", "10:00:00"),
                   fengdan_amt=kw.pop("fengdan_amt", 1e8),
                   float_mv=kw.pop("float_mv", 5e9),
                   amount=kw.pop("amount", 5e8),
                   turnover=kw.pop("turnover", 10.0),
                   lianban_hint=height_hint, **kw)


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config.load()
        self.db = DB(Path(self.tmp.name) / "test.db")
        self.repo = SnapshotRepo(self.db)
        self.ovr = OverrideRepo(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_lianban_rules(self):
        tracker = LianbanTracker(self.cfg)
        # D1：A/B/C 涨停（C 是 ST），首板
        d1 = [
            make_stock("000001", "甲", D1),
            make_stock("000002", "乙", D1, is_yizi=True),
            make_stock("000003", "ST丙", D1, is_st=True),
        ]
        state1 = tracker.update(D1, d1, {}, [])
        heights = {s["code"]: s["height"] for s in state1}
        self.assertEqual(heights, {"000001": 1, "000002": 1, "000003": 1})  # ST 也计入连板
        yizi = {s["code"]: s["yizi_count"] for s in state1}
        self.assertEqual(yizi["000002"], 1)

        # D2：A 晋级 2 板，B 断板清零，D 新首板
        d2 = [make_stock("000001", "甲", D2), make_stock("000004", "丁", D2)]
        state2 = tracker.update(D2, d2, {s["code"]: s for s in state1},
                                [D1.isoformat()])
        h2 = {s["code"]: s["height"] for s in state2}
        self.assertEqual(h2, {"000001": 2, "000004": 1})  # B 断板消失

        # D2 重复运行：幂等，A 不会变 3 板
        state2b = tracker.update(D2, d2, {s["code"]: s for s in state2},
                                 [D1.isoformat(), D2.isoformat()])
        self.assertEqual({s["code"]: s["height"] for s in state2b}, h2)

        # 种子机制：无历史时用数据源连板数
        d3 = [make_stock("000009", "戊", D2, height_hint=3)]
        seeded = tracker.update(D2, d3, {}, [])
        self.assertEqual(seeded[0]["height"], 3)

    def test_cluster_and_rank(self):
        # 造概念：AI 概念 3 只涨停（达标），军工 1 只（min_size=1 单股也成板块）
        blocks = [
            ConceptBlock("AI算力", ["000001", "000002", "000005"]),
            ConceptBlock("军工", ["000009"]),
        ]
        self.repo.rebuild_concepts(blocks)

        stocks = [
            vars(make_stock("000001", "甲", D2, zt_time="09:31:00",
                            fengdan_amt=5e8, float_mv=5e9)) | {"lianban_height": 2, "sealed": 1},
            vars(make_stock("000002", "乙", D2, zt_time="10:05:00",
                            fengdan_amt=2e8, float_mv=8e9)) | {"lianban_height": 1, "sealed": 1},
            vars(make_stock("000005", "戊", D2, zt_time="09:26:00", is_yizi=True,
                            fengdan_amt=3e8, float_mv=4e9)) | {"lianban_height": 1, "sealed": 1},
            vars(make_stock("000009", "壬", D2)) | {"lianban_height": 1, "sealed": 1},
        ]
        clusterer = StaticConceptClusterer(self.cfg, self.repo)
        clusters = clusterer.cluster(stocks, [])
        self.assertIn("AI算力", clusters)
        self.assertIn("军工", clusters)  # min_size=1：单股也成板块，不再剔除
        self.assertEqual(len(clusters["AI算力"]), 3)

        ranker = LeaderRanker(self.cfg)
        ranked = ranker.rank(clusters["AI算力"])
        # 板幅归一后封板股恒 1.0：同强度内先比上板时间——
        # 戊一字板 09:26 最早 -> 龙1，甲 09:31 次之，乙 10:05 最后
        self.assertEqual(ranked[0]["code"], "000005")
        self.assertEqual(ranked[0]["leader_label"], "龙1")
        self.assertEqual(ranked[1]["code"], "000001")

    def test_rank_order_strength_time_height(self):
        """龙几排序：板幅归一强度（封板恒1.0）> 上板时间 > 连板高度；
        创/科异动股无上板时间，按涨幅/板幅排在封板股之后"""
        ranker = LeaderRanker(self.cfg)
        base = dict(fengdan_amt=0.0, float_mv=1e9, turnover=5.0)
        members = [
            dict(code="000001", name="甲", sealed=1, lianban_height=1, zt_time="09:30", high_pct=10.0, **base),
            dict(code="000002", name="乙", sealed=1, lianban_height=1, zt_time="09:35", high_pct=10.0, **base),
            dict(code="300001", name="丙", sealed=2, lianban_height=0, zt_time="", high_pct=18.9, **base),
            dict(code="688001", name="丁", sealed=1, lianban_height=2, zt_time="14:50", high_pct=20.0, **base),
        ]
        order = [x["code"] for x in ranker.rank(members)]
        # 封板恒 1.0，按上板时间：甲09:30 > 乙09:35 > 丁14:50；
        # 丙 20cm 异动 18.9/20=0.945 < 1.0，排封板股之后
        self.assertEqual(order, ["000001", "000002", "688001", "300001"])

    def test_rank_board_strength_unsealed(self):
        """板幅归一：同为创/科异动时按涨幅/板幅排序（谁先到 9.90 谁靠前）"""
        ranker = LeaderRanker(self.cfg)
        base = dict(fengdan_amt=0.0, float_mv=1e9, turnover=5.0, zt_time="")
        members = [
            dict(code="300001", name="丙", sealed=2, lianban_height=0, high_pct=18.9, **base),
            dict(code="300002", name="丁", sealed=2, lianban_height=0, high_pct=13.0, **base),
        ]
        order = [x["code"] for x in ranker.rank(members)]
        self.assertEqual(order, ["300001", "300002"])  # 0.945 > 0.65

    def test_override_layer(self):
        self.db.execute(
            "INSERT INTO leader_result (trade_date, theme_name, code, name, "
            "algo_rank, final_rank, lianban_height) VALUES (?,?,?,?,?,?,?)",
            (D2.isoformat(), "AI算力", "000001", "甲", 1, 1, 2),
        )
        self.ovr.rerank(D2, "AI算力", "000001", 2)
        r = self.db.query(
            "SELECT algo_rank, final_rank, is_override FROM leader_result "
            "WHERE code='000001'"
        )[0]
        self.assertEqual(r["algo_rank"], 1)   # 算法结果不被覆盖
        self.assertEqual(r["final_rank"], 2)
        self.assertEqual(r["is_override"], 1)
        self.assertEqual(self.ovr.daily_intervention_count(D2), 1)

    def test_reset_layer(self):
        """复原：软删除移出 + 重排，一键恢复到算法结果"""
        self.db.execute(
            "INSERT INTO leader_result (trade_date, theme_name, code, name, "
            "algo_rank, final_rank, lianban_height) VALUES (?,?,?,?,?,?,?)",
            (D2.isoformat(), "AI算力", "000001", "甲", 1, 1, 2),
        )
        self.db.execute(
            "INSERT INTO leader_result (trade_date, theme_name, code, name, "
            "algo_rank, final_rank, lianban_height) VALUES (?,?,?,?,?,?,?)",
            (D2.isoformat(), "AI算力", "000002", "乙", 2, 2, 1),
        )
        # 操作失误：把甲重排到第 2、把乙移出
        self.ovr.rerank(D2, "AI算力", "000001", 2)
        self.ovr.move_out(D2, "AI算力", "000002")

        r = self.db.query(
            "SELECT is_removed FROM leader_result WHERE code='000002'")[0]
        self.assertEqual(r["is_removed"], 1)  # 软删除生效

        n = self.ovr.reset_theme(D2, "AI算力")
        self.assertGreaterEqual(n, 2)
        for row in self.db.query(
                "SELECT * FROM leader_result WHERE theme_name='AI算力'"):
            self.assertEqual(row["final_rank"], row["algo_rank"])
            self.assertEqual(row["is_override"], 0)
            self.assertEqual(row["is_removed"], 0)


if __name__ == "__main__":
    unittest.main()
