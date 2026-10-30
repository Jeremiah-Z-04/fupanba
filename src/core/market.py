"""A 股交易日状态判断。

从根源杜绝「未收盘的今天被当成最终数据」：
- future（未来日期）：无效，禁止采集
- holiday（周末/法定节假日休市）：无当日数据，调用方应回退到上一交易日
- preopen（今天盘前，<09:25）：涨停池无今天数据，接口会返回上一交易日脏数据，禁止采集
- intraday（今天盘中，09:25~15:00）：涨停池为实时快照，收盘后会变，允许采集但需标记
- closed（历史或今天已收盘 >=15:00）：最终数据，可正常采集/复盘

休市判定统一走 src/core/trading_calendar.py，本模块只负责「时间维度」的状态。
"""
from __future__ import annotations

from datetime import date, datetime, time

from src.core.trading_calendar import get_calendar

OPEN_TIME = time(9, 25)    # 集合竞价结束，涨停池开始有当日数据
CLOSE_TIME = time(15, 0)   # A 股收盘


def market_status(trade_date: date) -> str:
    """返回交易日市场状态：future / holiday / preopen / intraday / closed。"""
    today = date.today()
    if trade_date > today:
        return "future"
    if not get_calendar().is_trading_day(trade_date):
        return "holiday"
    if trade_date < today:
        return "closed"
    now = datetime.now().time()
    if now >= CLOSE_TIME:
        return "closed"
    if now >= OPEN_TIME:
        return "intraday"
    return "preopen"


def is_final(trade_date: date) -> bool:
    """该交易日的数据是否已是最终（已收盘）口径。"""
    return market_status(trade_date) == "closed"
