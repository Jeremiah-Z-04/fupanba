"""A 股交易日历 —— 休市判定 + 上一交易日回溯（全项目唯一出口）

为什么需要它：
  同花顺池类接口在非交易日返回**空列表**，直接采集会把「休市」误判成
  「0 涨停 / 0 炸板」，还会覆盖连板状态。所以采集/复盘前必须先判休市，
  并把休市日回退到上一交易日。

数据来源（按优先级，逐级兜底，任何一级挂了都不影响主链路）：
  1. 同花顺交易日历 `/api/a-share/calendar/trading-days`（近一年，滚动窗口）
  2. data/trading_days.json 本地缓存（接口抖动/离线时用）
  3. 周一~周五兜底（无任何日历数据时，至少周末判定正确）

边界处理（关键，别改成「日历里没有就是休市」）：
  - 日历是**滚动近一年**且只到最近一个交易日，今天之后没有任何日期。
    所以「日期 > 日历末日」不能判休市，否则每个正常交易日都会被误判。
  - 日历窗口内（min ~ max）缺失的工作日 = 法定节假日，可确定判休市。
  - 今天超出日历末日时：收盘后（>=15:05）用上证指数日 K 是否已有今日
    这根线来定论；盘中/盘前则乐观视为交易日，绝不把当天数据藏起来。

对外 API：
    cal = get_calendar()
    cal.is_trading_day(d)      # 是否交易日
    cal.prev_trading_day(d)    # 上一交易日（严格早于 d）
    cal.latest_trading_day(d)  # d 或之前最近的一个交易日
    cal.resolve(d)             # 统一解析：请求日 / 实际数据日 / 休市原因
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from src.config import PROJECT_ROOT
from src.log import get_logger

log = get_logger(__name__)

_TZ_SH = timezone(timedelta(hours=8))

CACHE_TTL_SEC = 6 * 3600          # 内存/文件日历的有效期
CACHE_MAX_AGE_DAYS = 7            # 文件缓存超过 7 天强制回源
_BAR_TTL_SEC = 600                # 「今日是否有 K 线」结论缓存 10 分钟
_MAX_BACK_DAYS = 30               # 上一交易日最多回溯 30 天（覆盖春节/国庆长假）
_AFTER_CLOSE = (15, 5)            # 收盘 15:00 + 5 分钟缓冲，日 K 落库后再判定

DEFAULT_CACHE = PROJECT_ROOT / "data" / "trading_days.json"


# ------------------------------------------------------------------ 工具

def _safe_date(value) -> date | None:
    try:
        return _as_date(value)
    except Exception:
        return None


def _as_date(value) -> date:
    """接受 date / datetime / '2026-10-24' / '20261024'。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    compact = s.replace("-", "")
    if len(compact) == 8 and compact.isdigit():
        return date(int(compact[:4]), int(compact[4:6]), int(compact[6:]))
    return date.fromisoformat(s)


def _now_sh() -> datetime:
    return datetime.now(_TZ_SH)


def _fetch_from_fuyao() -> list[str]:
    """同花顺交易日历（近一年）。失败返回空列表，由上层兜底。"""
    try:
        from src.datasources import fuyao_api as fy

        return fy.trading_days() or []
    except Exception as e:  # 数据源不可用不该拖垮休市判定
        log.warning("[calendar] 交易日历拉取失败: %s", e)
        return []


# ------------------------------------------------------------------ 主体

class TradingCalendar:
    """交易日历（线程安全，内存 + 文件双层缓存）。

    :param cache_file: 本地缓存路径，默认 data/trading_days.json
    :param fetcher: 远程取数函数（返回 yyyyMMdd 列表），测试可注入
    :param today_fn: 当前时间函数（返回 datetime），测试可注入
    """

    def __init__(
        self,
        cache_file: Path | str | None = None,
        fetcher: Callable[[], Iterable[str]] | None = None,
        today_fn: Callable[[], datetime] | None = None,
    ):
        self.cache_file = Path(cache_file) if cache_file else DEFAULT_CACHE
        self._fetcher = fetcher or _fetch_from_fuyao
        self._now = today_fn or _now_sh
        self._lock = threading.RLock()
        self._days: frozenset[date] | None = None
        self._loaded_at = 0.0
        self._bar_cache: dict[date, tuple[float, bool | None]] = {}

    # ------------------------------------------------ 日历数据
    def days(self) -> list[date]:
        """升序交易日列表（近一年）。"""
        self._ensure_loaded()
        return sorted(self._days or frozenset())

    def _ensure_loaded(self) -> None:
        with self._lock:
            if self._days is not None and time.time() - self._loaded_at < CACHE_TTL_SEC:
                return
            cached, cached_ts = self._read_cache()
            fresh = cached and (time.time() - cached_ts) < CACHE_TTL_SEC
            if fresh:
                self._days, self._loaded_at = cached, time.time()
                return

            today = self._now().date()
            remote: frozenset[date] = frozenset()
            need_remote = (
                not cached
                or (time.time() - cached_ts) > CACHE_MAX_AGE_DAYS * 86400
                # 今天比日历末日还新：可能是新交易日，也可能是节假日，回源确认
                or (today.weekday() < 5 and today > max(cached))
            )
            if need_remote:
                remote = self._fetch()
            if remote:
                self._days = remote
                self._write_cache(remote)
            else:
                self._days = cached or frozenset()
            self._loaded_at = time.time()

    def _fetch(self) -> frozenset[date]:
        try:
            raw = self._fetcher() or []
        except Exception as e:
            log.warning("[calendar] 交易日历拉取异常，使用本地缓存兜底: %s", e)
            return frozenset()
        out = {d for d in (_safe_date(x) for x in raw) if d}
        if out:
            log.info("[calendar] 交易日历已更新: %s ~ %s（%d 个交易日）",
                     min(out), max(out), len(out))
        return frozenset(out)

    def _read_cache(self) -> tuple[frozenset[date], float]:
        try:
            raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except Exception:
            return frozenset(), 0.0
        days = frozenset(filter(None, (_safe_date(x) for x in raw.get("days") or [])))
        try:
            ts = datetime.fromisoformat(str(raw.get("updated_at"))).timestamp()
        except Exception:
            ts = 0.0
        return days, ts

    def _write_cache(self, days: frozenset[date]) -> None:
        payload = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "source": "hithink",
            "count": len(days),
            "days": [d.isoformat() for d in sorted(days)],
        }
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_name(self.cache_file.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cache_file)
        except Exception as e:
            log.warning("[calendar] 交易日历缓存写入失败（不影响本次运行）: %s", e)

    # ------------------------------------------------ 休市判定
    def is_trading_day(self, day) -> bool:
        d = _as_date(day)
        if d.weekday() >= 5:          # 周末永远休市，不依赖任何接口
            return False

        self._ensure_loaded()
        days = self._days or frozenset()
        if d in days:
            return True

        now = self._now()
        today = now.date()
        if days:
            lo, hi = min(days), max(days)
            if lo <= d <= hi:         # 日历覆盖范围内缺失的工作日 = 法定节假日
                return False

        if d == today and (now.hour, now.minute) >= _AFTER_CLOSE:
            has_bar = self._today_has_bar(d)
            if has_bar is not None:
                return has_bar

        # 日历覆盖不到（超出滚动窗口/接口不可用）：乐观视为交易日，
        # 宁可让采集去试一次，也不要把正常交易日的数据误判成休市藏起来。
        return True

    def prev_trading_day(self, day) -> date | None:
        d = _as_date(day)
        for i in range(1, _MAX_BACK_DAYS + 1):
            cand = d - timedelta(days=i)
            if self.is_trading_day(cand):
                return cand
        return None

    def latest_trading_day(self, day) -> date:
        d = _as_date(day)
        if self.is_trading_day(d):
            return d
        return self.prev_trading_day(d) or d

    def holiday_kind(self, day) -> str:
        """休市原因：weekend（周末）/ holiday（节假日）/ ''（非休市）。"""
        d = _as_date(day)
        if self.is_trading_day(d):
            return ""
        return "weekend" if d.weekday() >= 5 else "holiday"

    def resolve(self, day) -> dict:
        """统一解析：把「用户选的日期」映射成「实际可用的交易日」。

        :return: {requested, trade_date, is_trading_day, is_holiday,
                  holiday_kind, adjusted, label}
        """
        d = _as_date(day)
        today = self._now().date()
        trading = self.is_trading_day(d)
        kind = "" if trading else ("weekend" if d.weekday() >= 5 else "holiday")

        effective = d
        if not trading and d <= today:
            effective = self.prev_trading_day(d) or d

        return {
            "requested": d,
            "trade_date": effective,
            "is_trading_day": trading,
            "is_holiday": not trading,
            "holiday_kind": kind,
            "adjusted": effective != d,
            # 日期展示文案：休市日带括号标注，如 2026-10-24（休市中）
            "label": f"{d.isoformat()}（休市中）" if not trading else d.isoformat(),
        }

    # ------------------------------------------------ 当日是否开市（K 线兜底）
    def _today_has_bar(self, d: date) -> bool | None:
        """上证指数当天是否已有日 K 线。None = 接口失败，无法判定。"""
        with self._lock:
            hit = self._bar_cache.get(d)
            if hit and time.time() - hit[0] < _BAR_TTL_SEC:
                return hit[1]

        result: bool | None = None
        try:
            from src.datasources import fuyao_api as fy

            start = fy.sh_date_ms((d - timedelta(days=10)).isoformat())
            end = fy.sh_date_ms(d.isoformat()) + 86_399_999
            items = fy.index_daily("000001.SH", start, end)
            if items:
                bar_days = {
                    datetime.fromtimestamp(it["date_ms"] / 1000, _TZ_SH).date()
                    for it in items if it.get("date_ms")
                }
                result = d in bar_days
        except Exception as e:
            log.warning("[calendar] 指数日 K 校验失败，按交易日处理: %s", e)

        with self._lock:
            self._bar_cache[d] = (time.time(), result)
        if result is False:
            log.info("[calendar] %s 无指数日 K，判定为休市", d)
        return result


# ------------------------------------------------------------------ 单例

_CAL_LOCK = threading.Lock()
_CALENDARS: dict[Path, TradingCalendar] = {}


def get_calendar(cache_dir: Path | str | None = None) -> TradingCalendar:
    """按缓存目录取共享实例（同一进程内不重复拉日历）。"""
    path = Path(cache_dir) / "trading_days.json" if cache_dir else DEFAULT_CACHE
    with _CAL_LOCK:
        cal = _CALENDARS.get(path)
        if cal is None:
            cal = TradingCalendar(path)
            _CALENDARS[path] = cal
        return cal
