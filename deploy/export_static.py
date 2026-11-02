# -*- coding: utf-8 -*-
"""把复盘结果导出成静态 JSON，供 GitHub Pages 上的只读版使用。

用法：
    python deploy/export_static.py                # 导出全部历史日期
    python deploy/export_static.py --days 60      # 只导最近 60 个交易日
    python deploy/export_static.py --out docs     # 指定输出目录

产出：
    docs/data/index.json          日期列表 + 生成时间
    docs/data/YYYY-MM-DD.json     单日完整数据（题材/龙头/宽度/未归纳/成分股）

查询口径与 src/server/app.py 里的 /api/* 完全一致——以后改接口记得同步这里。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Config            # noqa: E402
from src.core.trading_calendar import get_calendar  # noqa: E402
from src.export import ths_files as ths  # noqa: E402
from src.storage.db import DB            # noqa: E402

TZ = timezone(timedelta(hours=8))

# 龙头表只留前端用得到的字段，别把整行塞进去（体积能小一半）
LEADER_FIELDS = ("code", "name", "theme_name", "final_rank", "algo_rank",
                 "lianban_height", "high_pct", "fengdan_amt", "float_mv",
                 "zt_time", "leader_label", "is_override")


def build_day(db: DB, cal, cfg: Config, ds: str) -> dict:
    d = datetime.strptime(ds, "%Y-%m-%d").date()

    themes = [dict(r) for r in db.query(
        "SELECT trade_date, theme_name, zt_count, strong_count, member_count, "
        "       cluster_mode, created_at FROM theme_result WHERE trade_date=? "
        "ORDER BY member_count DESC, zt_count DESC", (ds,))]

    leaders = []
    for r in db.query("SELECT * FROM leader_result WHERE trade_date=? AND is_removed=0 "
                      "ORDER BY theme_name, final_rank", (ds,)):
        row = dict(r)          # sqlite3.Row 没有 .get()，先转成 dict
        leaders.append({k: row.get(k) for k in LEADER_FIELDS})

    zt = db.query("SELECT COUNT(*) c FROM daily_zt_pool WHERE trade_date=? AND sealed=1",
                  (ds,))[0]["c"]
    row = db.query("SELECT * FROM market_breadth WHERE trade_date=?", (ds,))
    if row:
        r = row[0]
        breadth = {"partial": False, "up_99": r["up_99"], "limit_up": r["limit_up"] or zt,
                   "tech_10": r["tech_10"], "total": r["total"],
                   "captured_at": r["captured_at"] or "", "hint": ""}
    else:
        breadth = {"partial": True, "up_99": None, "limit_up": zt, "tech_10": None,
                   "total": 0, "captured_at": "",
                   "hint": "该日没有全市场快照，只能给出涨停家数"}

    unassigned = [{
        "code": r["code"], "name": r["name"],
        "status": "涨停" if r["sealed"] == 1 else "创/科异动",
        "reason": r["reason"] or "",
        "concepts": (r["concepts"] or "").split(",") if r["concepts"] else [],
        "lianban_height": r["lianban_height"] or 0,
        "high_pct": r["high_pct"] or 0.0,
        "zt_time": r["zt_time"] or "",
    } for r in db.query(
        "SELECT p.code, p.name, p.sealed, p.reason, "
        "(SELECT GROUP_CONCAT(c.concept_name) FROM concept_member c "
        "  WHERE c.code=p.code) AS concepts, "
        "p.lianban_height, p.high_pct, p.zt_time "
        "FROM daily_zt_pool p "
        "WHERE p.trade_date=? AND p.sealed IN (1,2) "
        "  AND p.code NOT IN ("
        "    SELECT l.code FROM leader_result l WHERE l.trade_date=? AND l.is_removed=0"
        "  ) "
        "ORDER BY p.sealed, p.lianban_height DESC, p.zt_time", (ds, ds))]

    # 供页面「生成图片 / 生成 TXT / 复制」用：题材名 -> 成分股代码（已按龙头顺序排好）
    exports: dict[str, list[str]] = {}
    try:
        rows = ths.collect_codes(db, d)
        for theme, codes in ths.group_by_theme(rows):
            exports[theme] = codes
    except Exception:
        pass

    # 爆量题材：复用与 /api/volume/surge 完全相同的聚合函数，防止两处口径漂移
    burst = None
    try:
        from src.core import volume_surge as vs
        p = vs.build_payload(db, ds, cfg)
        if p.get("ready"):
            p["base_window"] = vs.prev_trading_days(
                cal, d, vs.vcfg(cfg)["base_window"])
            # 静态站是只读展示，裁掉前端用不到的字段（suspect 永远 false、
            # sub 没显示、is_zt 和 src 重复），单日体积能小两成
            keep = ("theme", "code", "name", "ratio", "volume",
                    "base_volume", "base_date", "high_pct", "src",
                    "reason", "reason_date")
            p["stocks"] = [{k: s.get(k) for k in keep} for s in p["stocks"]]
            burst = p
    except Exception:
        burst = None

    return {
        "date": ds,
        "trading_day": cal.is_trading_day(d),
        "breadth": breadth,
        "themes": themes,
        "leaders": leaders,
        "unassigned": {"count": len(unassigned), "stocks": unassigned},
        "exports": exports,
        "burst": burst,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只导最近 N 个交易日，0=全部")
    ap.add_argument("--out", default="docs", help="输出目录（相对项目根）")
    args = ap.parse_args()

    cfg = Config.load()
    db = DB(cfg.db_path)
    cal = get_calendar(cfg.data_dir)

    out_dir = ROOT / args.out / "data"
    out_dir.mkdir(parents=True, exist_ok=True)

    dates = [r["trade_date"] for r in db.query(
        "SELECT DISTINCT trade_date FROM theme_result ORDER BY trade_date DESC")]
    if args.days > 0:
        dates = dates[:args.days]
    if not dates:
        print("数据库里还没有复盘结果，先跑一次 main.py run")
        return 1

    total_bytes = 0
    for ds in dates:
        payload = build_day(db, cal, cfg, ds)
        p = out_dir / f"{ds}.json"
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        p.write_text(text, encoding="utf-8")
        total_bytes += len(text.encode("utf-8"))

    index = {
        "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "latest": dates[0],
        "dates": dates,
    }
    (out_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8")

    # 清掉不在本次范围内的旧日期文件（--days 缩小时不会留下孤儿文件）
    keep = set(dates)
    for f in out_dir.glob("*.json"):
        if f.stem.count("-") == 2 and f.stem not in keep:
            f.unlink()

    print(f"已导出 {len(dates)} 个交易日 -> {out_dir}")
    print(f"  最新：{dates[0]}    合计 {total_bytes / 1024:.0f} KB")
    print("\n下一步：")
    print("  python deploy/publish_static.py      # 提交并推送到 GitHub")
    return 0


if __name__ == "__main__":
    sys.exit(main())
