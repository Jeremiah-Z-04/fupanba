# -*- coding: utf-8 -*-
"""成交量口径探针（一次性，验证通过后可删）。

验证 6 件事：
  1. 全市场快照家数是否稳定 ≈5500，字段里有没有 volume
  2. 个股日K 端点是否正确（走错成 a-share-index 会静默返回空且 code=0）
  3. 日K 实际字段名
  4. volume 单位：股 or 手（用 turnover / (volume*close) 判定）
  5. 快照 volume 与日K volume 是否同口径（同一交易日直接相除）
  6. 指数端点对照（证明个股不能走指数端点）

用法：
    python deploy/probe_volume.py

判定标准：
    snapshot/bar 在 0.95~1.05  => 同口径，方案可直接落地
    接近 0.01 或 100            => 单位差异，落库时统一乘换算系数
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.datasources import fuyao_api as fy  # noqa: E402

# 覆盖沪主板 / 深主板 / 创业板 / 科创板
CODES = ["600519.SH", "000001.SZ", "300750.SZ", "688981.SH"]


def unit_guess(volume, close, turnover):
    """用 turnover/(volume*close) 反推 volume 单位。"""
    if not volume or not close or not turnover:
        return "字段缺失"
    r = turnover / (volume * close)
    if abs(r - 1) < 0.15:
        return "股 (r=%.3f)" % r
    if abs(r - 100) < 15:
        return "手 (r=%.3f, 1手=100股)" % r
    return "未知口径 (r=%.3f)" % r


def main():
    print("=" * 74)
    print("API key 已配置:", bool(fy.is_configured()))
    if not fy.is_configured():
        print("!! .env 里没有 HITHINK_FINANCE_API_KEY，无法探测")
        return

    days = fy.trading_days(force=True)
    if not days:
        print("!! 交易日历拉取失败:", fy.last_error)
        return
    print("交易日历: %d 天  %s ~ %s" % (len(days), days[0], days[-1]))

    # ---------- 1. 全市场快照 ----------
    print("-" * 74)
    snap = fy.all_a_share_snapshot(force=True) or {}
    print("全市场快照家数: %d   (预期 >=5300)" % len(snap))
    if not snap:
        print("!! 快照为空:", fy.last_error)
        return
    k = next(iter(snap))
    print("快照字段:", sorted(snap[k].keys()))
    print("快照样例:", json.dumps(
        {x: snap[k].get(x) for x in
         ("ticker", "last_price", "prev_price", "volume", "turnover")},
        ensure_ascii=False))

    latest = days[-1]
    end = fy.sh_date_ms(latest) + 86_399_999
    start = fy.sh_date_ms(days[-15] if len(days) >= 15 else days[0])

    ok_cnt = bad_cnt = empty_cnt = 0
    print("-" * 74)
    print("日K 窗口: %s ~ %s" % (days[-15] if len(days) >= 15 else days[0], latest))

    for code in CODES:
        print("-" * 74)
        print("代码:", code)
        bars = fy.stock_daily(code, start_ms=start, end_ms=end, force=True)
        if bars is None:
            bad_cnt += 1
            print("  日K -> None（端点错 / 限流 / 参数错）", fy.last_error)
            continue
        if not bars:
            empty_cnt += 1
            print("  *** 空数据：极可能走了 a-share-index 端点（会静默返空）***")
            continue
        ok_cnt += 1
        print("  日K 根数:", len(bars), " 区间:", bars[0]["date"], "~", bars[-1]["date"])
        print("  bar 字段:", sorted(bars[-1].keys()))
        b = bars[-1]
        print("  最后一根:", json.dumps(b, ensure_ascii=False))
        print("  日K 单位判定:", unit_guess(b["volume"], b["close"], b["turnover"]))

        it = snap.get(code) or (snap.get(code.split(".")[0]))
        if not it:
            print("  快照里没有这只(可能停牌/退市)，跳过同口径对比")
            continue
        sv = float(it.get("volume") or 0)
        ratio = (sv / b["volume"]) if b["volume"] else 0
        print("  快照 volume=%s   日K volume=%s   snapshot/bar=%.4f"
              % (sv, b["volume"], ratio))
        print("  快照单位判定:", unit_guess(
            sv, float(it.get("last_price") or 0), float(it.get("turnover") or 0)))
        if 0.95 <= ratio <= 1.05:
            print("  => 同口径（股），可直接落库")
        elif ratio:
            print("  => 口径不一致！落库前需统一乘 %.4f" % (1 / ratio if ratio else 0))

    # ---------- 2. 指数端点对照 ----------
    print("-" * 74)
    idx = fy.index_daily("000001.SH", start, end)
    print("指数端点 index_daily(000001.SH) 根数:", len(idx or []))
    print("（个股若走这个端点会返回空，以此证明端点不能混用）")

    # ---------- 3. 汇总 ----------
    print("=" * 74)
    print("汇总: 日K 成功 %d / 空 %d / 失败 %d" % (ok_cnt, empty_cnt, bad_cnt))
    if ok_cnt and not empty_cnt and not bad_cnt:
        print("探针通过：接口可用，按上方 snapshot/bar 比值决定是否换算单位。")
    else:
        print("探针未通过，先解决接口/端点问题再继续。")
    print("=" * 74)


if __name__ == "__main__":
    main()
