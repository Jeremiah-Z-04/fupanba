"""全市场情绪宽度：涨幅 >=9.90% / 涨停 / 创科北 >10%

三个数的口径：
  up_99   全市场涨幅 >=9.90% 的家数（强势股总数，含已封板）
  limit_up 题材池里的涨停家数（封板口径，见 daily_zt_pool.sealed=1）
  tech_10 创业板/科创板/北交所 涨幅 >10% 的家数（20cm、30cm 的强势股，
          注意它们 >10% 未必涨停，涨停的 20cm 会在 limit_up 里）

数据来源是全市场快照（实时），所以只能在采集当日计算；历史日期没有快照，
接口返回 partial，前端显示「—」，不拿池子里的数蒙一个出来。
"""
from __future__ import annotations

from src.datasources.base import normalize_code

UP_THRESHOLD = 9.90
TECH_THRESHOLD = 10.0
TECH_PREFIX = ("30", "68", "92", "43", "83", "87", "88")


def _pct(item: dict) -> float:
    try:
        return float(item.get("price_change_ratio_pct") or 0)
    except (TypeError, ValueError):
        return 0.0


def compute(snapshot: dict) -> dict:
    """从全市场快照算三个数。snapshot: {ticker: {price_change_ratio_pct, ...}}"""
    up_99 = tech_10 = total = 0
    for ticker, item in (snapshot or {}).items():
        code = normalize_code(str(ticker))
        if len(code) != 6:
            continue
        pct = _pct(item or {})
        total += 1
        if pct >= UP_THRESHOLD:
            up_99 += 1
        if code[:2] in TECH_PREFIX and pct > TECH_THRESHOLD:
            tech_10 += 1
    return {"up_99": up_99, "tech_10": tech_10, "total": total}
