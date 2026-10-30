"""交易日历 / 休市回退 单测（离线，注入日历与当前时间，不碰行情接口）

覆盖：
  1. 周末休市 → 回退上一交易日（周五）
  2. 日历窗口内的法定节假日 → 回退节前最后一个交易日
  3. 日历滚动窗口外的工作日 → 乐观视为交易日（不能把正常交易日误判成休市）
  4. 收盘后用指数日 K 兜底：当天无 K 线 = 休市
  5. 交易日原样返回，不产生 adjusted
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.core.trading_calendar import TradingCalendar

TZ = timezone(timedelta(hours=8))

DAYS = ["20260928", "20260929", "20260930",
        "20261008", "20261009",
        "20261019", "20261020", "20261021", "20261022", "20261023"]


def cal_at(now: datetime, days=None) -> TradingCalendar:
    tmp = tempfile.mkdtemp(prefix="th_cal_")
    return TradingCalendar(
        cache_file=Path(tmp) / "trading_days.json",
        fetcher=lambda: days if days is not None else DAYS,
        today_fn=lambda: now,
    )


class TradingCalendarTest(unittest.TestCase):
    def test_weekend_falls_back_to_friday(self):
        cal = cal_at(datetime(2026, 10, 24, 11, 0, tzinfo=TZ))   # 周六
        self.assertFalse(cal.is_trading_day(date(2026, 10, 24)))
        info = cal.resolve(date(2026, 10, 24))
        self.assertEqual(info["trade_date"], date(2026, 10, 23))
        self.assertEqual(info["holiday_kind"], "weekend")
        self.assertTrue(info["adjusted"])
        self.assertEqual(info["label"], "2026-10-24（休市中）")

    def test_legal_holiday_inside_window(self):
        # 国庆 10-01~10-07 不在日历里 → 回退到 09-30
        cal = cal_at(datetime(2026, 10, 9, 20, 0, tzinfo=TZ))
        self.assertFalse(cal.is_trading_day(date(2026, 10, 5)))
        info = cal.resolve(date(2026, 10, 5))
        self.assertEqual(info["trade_date"], date(2026, 9, 30))
        self.assertEqual(info["holiday_kind"], "holiday")
        self.assertEqual(info["label"], "2026-10-05（休市中）")

    def test_beyond_window_weekday_is_trading(self):
        # 日历只到 10-23，10-26（周一）超出窗口 → 乐观视为交易日，不能判休市
        cal = cal_at(datetime(2026, 10, 26, 10, 0, tzinfo=TZ))
        self.assertTrue(cal.is_trading_day(date(2026, 10, 26)))
        info = cal.resolve(date(2026, 10, 26))
        self.assertFalse(info["adjusted"])
        self.assertEqual(info["trade_date"], date(2026, 10, 26))
        self.assertEqual(info["holiday_kind"], "")

    def test_after_close_uses_index_bar(self):
        class NoBarCalendar(TradingCalendar):
            def _today_has_bar(self, d):
                return False

        tmp = tempfile.mkdtemp(prefix="th_cal_")
        cal = NoBarCalendar(
            cache_file=Path(tmp) / "trading_days.json",
            fetcher=lambda: DAYS,
            today_fn=lambda: datetime(2026, 10, 26, 16, 0, tzinfo=TZ),
        )
        self.assertFalse(cal.is_trading_day(date(2026, 10, 26)))
        self.assertEqual(cal.resolve(date(2026, 10, 26))["trade_date"],
                         date(2026, 10, 23))

    def test_prev_trading_day_skips_weekend(self):
        cal = cal_at(datetime(2026, 10, 26, 10, 0, tzinfo=TZ))
        self.assertEqual(cal.prev_trading_day(date(2026, 10, 26)),
                         date(2026, 10, 23))
        self.assertEqual(cal.prev_trading_day(date(2026, 10, 24)),
                         date(2026, 10, 23))

    def test_trading_day_untouched(self):
        cal = cal_at(datetime(2026, 10, 23, 20, 0, tzinfo=TZ))
        info = cal.resolve(date(2026, 10, 23))
        self.assertTrue(info["is_trading_day"])
        self.assertFalse(info["is_holiday"])
        self.assertEqual(info["trade_date"], date(2026, 10, 23))
        self.assertEqual(info["label"], "2026-10-23")


if __name__ == "__main__":
    unittest.main()
