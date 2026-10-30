"""连板高度计算 —— 全项目唯一口径出口（规则集中在此，改口径只动本文件）

口径（已与产品方锁定）:
  1. 断板即清零：昨日连板股今日未封板 -> 移出状态表
  2. ST 股涨停不累计连板高度（st_not_counted）
  3. 一字板打标并计数（yizi_count），供排序因子使用
  4. 连板延续判定：上次涨停日 == 上一个交易日（交易日历用本库已有数据推导，
     不依赖外部日历，工具哪天没跑也不影响判定）

首次运行无历史数据时，用数据源返回的连板数（akshare 东财接口自带）做种子，
之后每日完全自维护。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.config import Config
from src.datasources.base import ZTStock
from src.log import get_logger

log = get_logger(__name__)


@dataclass
class LianbanEntry:
    code: str
    name: str
    height: int
    last_zt_date: str
    yizi_count: int

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name, "height": self.height,
            "last_zt_date": self.last_zt_date, "yizi_count": self.yizi_count,
        }


class LianbanTracker:
    def __init__(self, cfg: Config):
        self.exclude_st = cfg.get("lianban.st_not_counted", True)

    def update(
        self,
        trade_date: date,
        today_zt: list[ZTStock],
        prev_state: dict[str, dict],
        known_trade_dates: list[str],
    ) -> list[dict]:
        """盘后更新连板状态。

        :param today_zt: 当日封住涨停的个股（sealed=True）
        :param prev_state: 昨日连板状态 {code: row}
        :param known_trade_dates: 库中已有全部交易日（ISO 字符串，升序）
        :return: 新状态列表（直接交给 SnapshotRepo.save_lianban_state）
        """
        today_iso = trade_date.isoformat()
        prev_trade_date = self._prev_trade_date(today_iso, known_trade_dates)

        # 无历史：用数据源自带的连板数做种子
        seeded = not prev_state and not known_trade_dates
        if seeded:
            log.info("首次运行，用数据源连板数做初始种子")

        new_state: list[dict] = []
        for s in today_zt:
            if self.exclude_st and s.is_st:
                continue
            prev = prev_state.get(s.code)
            hint = getattr(s, "lianban_hint", 0) or 0

            if prev and prev["last_zt_date"] == prev_trade_date:
                height = prev["height"] + 1
                yizi = prev["yizi_count"] + (1 if s.is_yizi else 0)
            elif prev and prev["last_zt_date"] == today_iso:
                # 同日重复运行：幂等，不重复累计
                height, yizi = prev["height"], prev["yizi_count"]
            elif seeded and hint > 0:
                height, yizi = hint, (hint if s.is_yizi else 0)
            else:
                # 上次涨停不在上一交易日 -> 中间断过板 -> 重新从 1 计
                height, yizi = 1, (1 if s.is_yizi else 0)

            new_state.append(LianbanEntry(
                code=s.code, name=s.name, height=height,
                last_zt_date=today_iso, yizi_count=yizi,
            ).to_dict())

        # 未出现在今日涨停池的昨日连板股 -> 断板清零（不写入新状态即清零）
        broken = set(prev_state) - {s["code"] for s in new_state}
        if broken:
            log.info("断板清零 %d 只: %s", len(broken), ",".join(sorted(broken)))

        new_state.sort(key=lambda x: -x["height"])
        return new_state

    @staticmethod
    def _prev_trade_date(today_iso: str, known_dates: list[str]) -> str:
        """上一个交易日 = 库中小于今日的最大日期（工具没跑的日子自动跳过）"""
        earlier = [d for d in known_dates if d < today_iso]
        return earlier[-1] if earlier else ""
