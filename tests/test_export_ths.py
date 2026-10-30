"""同花顺导出通道测试（离线，不碰网络、不碰浏览器）

覆盖三件容易出错的事：
  1. 文件名：题材名带 / : " 等非法字符、超长、撞名
  2. txt 内容：纯 6 位代码 + CRLF + ASCII 无 BOM（GBK/UTF-8 读出来一致）
  3. 分组口径：题材内去重、跨题材允许重复、单只题材默认跳过
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.config import Config
from src.export import ths_files as ths
from src.storage.db import DB

D1 = date(2026, 10, 27)


class FilenameTest(unittest.TestCase):
    def test_illegal_chars(self):
        self.assertEqual(ths.sanitize_filename("AI/算力"), "AI_算力")
        self.assertEqual(ths.sanitize_filename('a:b*c?"d<e>f|g\\h'),
                         "a_b_c__d_e_f_g_h")

    def test_trailing_space_and_dot(self):
        self.assertEqual(ths.sanitize_filename("  期末. "), "期末")

    def test_empty(self):
        self.assertEqual(ths.sanitize_filename(""), "未命名")
        self.assertEqual(ths.sanitize_filename(None), "未命名")

    def test_too_long_keeps_hash(self):
        name = "机器" * 20
        out = ths.sanitize_filename(name)
        self.assertLessEqual(len(out), 24)
        self.assertRegex(out, r"-[0-9a-f]{4}$")
        self.assertNotEqual(out, ths.sanitize_filename(name + "x"))

    def test_unique_case_insensitive(self):
        out = ths.unique_names(["AI/算力", "AI\\算力", "ai_算力"])
        self.assertEqual(len(set(v.casefold() for v in out.values())), 3)


class TxtContentTest(unittest.TestCase):
    def test_crlf_no_bom(self):
        raw = ths.build_txt_text(["600000", "000001"])
        self.assertEqual(raw, b"600000\r\n000001")
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))

    def test_encoding_agnostic(self):
        raw = ths.build_txt_text(["600000", "000001"])
        self.assertEqual(raw.decode("gbk"), raw.decode("utf-8"))

    def test_plan_single_file_by_default(self):
        codes = [f"{i:06d}" for i in range(18)]
        plan = ths.plan_txt_batches("机器人", codes, "2026-10-27")
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["name"], "机器人_2026-10-27.txt")
        self.assertEqual(plan[0]["count"], 18)

    def test_plan_chunked(self):
        codes = [f"{i:06d}" for i in range(10)]
        plan = ths.plan_txt_batches("机器人", codes, "2026-10-27", chunk=6)
        self.assertEqual([p["name"] for p in plan],
                         ["机器人_2026-10-27_01of02.txt", "机器人_2026-10-27_02of02.txt"])
        self.assertEqual([p["count"] for p in plan], [6, 4])


class ImagePlanTest(unittest.TestCase):
    def test_eighteen_codes_split_by_twelve(self):
        codes = [f"{i:06d}" for i in range(18)]
        plan = ths.plan_image_batches("机器人", codes, "2026-10-27", 12)
        self.assertEqual(len(plan), 2)
        self.assertEqual([p["count"] for p in plan], [12, 6])
        self.assertEqual(plan[0]["name"], "机器人_2026-10-27_01of02.png")
        self.assertEqual(plan[1]["codes"], codes[12:])

    def test_empty_codes_no_images(self):
        self.assertEqual(ths.plan_image_batches("机器人", [], "2026-10-27"), [])


class GroupTest(unittest.TestCase):
    def test_dedup_inside_theme_only(self):
        rows = [
            {"theme": "A", "code": "600000"},
            {"theme": "A", "code": "600000"},
            {"theme": "B", "code": "600000"},
        ]
        groups = ths.group_by_theme(rows)
        self.assertEqual(groups, [("A", ["600000"]), ("B", ["600000"])])

    def test_filter_singletons(self):
        groups = [("A", ["1", "2"]), ("B", ["3"]), ("C", ["4", "5", "6"])]
        kept, skipped = ths.filter_themes(groups, min_members=2, max_themes=0)
        self.assertEqual([g[0] for g in kept], ["A", "C"])
        self.assertEqual([s["theme"] for s in skipped], ["B"])

    def test_filter_max_themes(self):
        groups = [(f"T{i}", ["1", "2"]) for i in range(5)]
        kept, skipped = ths.filter_themes(groups, min_members=2, max_themes=2)
        self.assertEqual(len(kept), 2)
        self.assertEqual(len(skipped), 3)

    def test_global_dedup_for_merged_file(self):
        self.assertEqual(
            ths.global_dedup([("A", ["1", "2"]), ("B", ["2", "3"])]),
            ["1", "2", "3"])


class PathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "exports"
        self.cfg = Config({"app": {"data_dir": "data"}, "export": {"ths": {"dir": "exports/ths"}}},
                          root=Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_peek_day_dir_does_not_create(self):
        d = ths.peek_day_dir(self.cfg, "2026-01-05")
        self.assertFalse(d.exists())
        self.assertEqual(d.name, "2026-01-05")

    def test_day_dir_created(self):
        d = ths.day_dir(self.cfg, "2026-10-27")
        self.assertTrue(d.is_dir())
        self.assertEqual(d.name, "2026-10-27")
        self.assertTrue(str(d).endswith("2026-10-27"))

    def test_write_and_read_back(self):
        d = ths.day_dir(self.cfg, "2026-10-27")
        p = ths.write_txt_file(d / "机器人_2026-10-27.txt", ["600000", "000001"])
        self.assertEqual(p.read_bytes(), b"600000\r\n000001")

    def test_ensure_inside(self):
        root = Path(self.tmp.name) / "exports"
        ok = root / "a.png"
        self.assertTrue(ths.ensure_inside(ok, root))
        with self.assertRaises(ValueError):
            ths.ensure_inside(root / ".." / "x.png", root)
        with self.assertRaises(ValueError):
            ths.ensure_inside(Path("/etc/passwd"), root)

    def test_delete_stale_txt_keeps_png(self):
        d = ths.day_dir(self.cfg, "2026-10-27")
        keep = ths.write_txt_file(d / "A.txt", ["600000"])
        stale = ths.write_txt_file(d / "老题材.txt", ["600001"])
        png = d / "A.png"
        png.write_bytes(b"x")
        removed = ths.delete_stale_txt(d, {keep.name})
        self.assertEqual(removed, 1)
        self.assertTrue(keep.exists())
        self.assertFalse(stale.exists())
        self.assertTrue(png.exists())

    def test_prune_old_dirs(self):
        root = ths.export_root(self.cfg)
        old = root / "2020-01-01"
        new = root / "2999-01-01"
        other = root / "_tmp"
        for p in (old, new, other):
            p.mkdir(parents=True, exist_ok=True)
        removed = ths.prune_old_dirs(root, keep_days=7)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertTrue(other.exists())


class CollectCodesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "test.db")
        rows = [
            ("2026-10-27", "大题材", 5, 5),
            ("2026-10-27", "小题材", 1, 1),
        ]
        for d, theme, zt, members in rows:
            self.db.execute(
                "INSERT INTO theme_result (trade_date, theme_name, zt_count, member_count) "
                "VALUES (?,?,?,?)", (d, theme, zt, members))
        members = [
            ("2026-10-27", "大题材", "600000", 1, 0),
            ("2026-10-27", "大题材", "000001", 2, 0),
            ("2026-10-27", "大题材", "300001", 3, 1),      # 人工移出
            ("2026-10-27", "小题材", "600002", 1, 0),
        ]
        for d, theme, code, rank, removed in members:
            self.db.execute(
                "INSERT INTO leader_result (trade_date, theme_name, code, final_rank, is_removed) "
                "VALUES (?,?,?,?,?)", (d, theme, code, rank, removed))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_soft_deleted_excluded_and_order(self):
        rows = ths.collect_codes(self.db, D1)
        self.assertEqual([r["theme"] for r in rows], ["大题材", "大题材", "小题材"])
        self.assertNotIn("300001", [r["code"] for r in rows])

    def test_theme_filter(self):
        rows = ths.collect_codes(self.db, D1, "小题材")
        self.assertEqual([r["code"] for r in rows], ["600002"])

    def test_dirty_code_dropped(self):
        self.db.execute(
            "INSERT INTO leader_result (trade_date, theme_name, code, final_rank) "
            "VALUES ('2026-10-27','大题材','1A0001',9)")
        rows = ths.collect_codes(self.db, D1, "大题材")
        self.assertTrue(all(len(r["code"]) == 6 and r["code"].isdigit() for r in rows))
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
