"""数据源抽象层：统一数据模型 + 接口定义

所有适配器（iFinD / akshare / wencai）实现同一接口，
上层（落库、打分、排序）只依赖本模块的数据模型，不感知具体来源。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date


@dataclass
class ZTStock:
    """涨停池个股快照（单日）"""
    code: str                  # 6 位代码，如 "000586"
    name: str
    trade_date: date
    zt_time: str = ""          # 首次封板时间 "HH:MM:SS"
    last_zt_time: str = ""     # 最后封板时间
    fengdan_amt: float = 0.0   # 封单金额（元）
    amount: float = 0.0        # 成交额（元）
    float_mv: float = 0.0      # 流通市值（元）
    turnover: float = 0.0      # 换手率（%）
    zhaban_count: int = 0      # 炸板次数
    high_pct: float = 0.0      # 日内最高涨幅（%）
    fall_pct: float = 0.0      # 自最高回落幅度（%）
    industry: str = ""         # 所属行业
    is_st: bool = False        # ST/退市
    is_yizi: bool = False      # 一字板（09:25 集合竞价即封板）
    is_bj: bool = False        # 北交所
    lianban_hint: int = 0      # 数据源返回的连板数（仅首次运行做种子用）
    reason: str = ""           # 涨停原因（同花顺 limit_up_reason，MVP4 LLM 命名用）
    anomaly_tag: str = ""      # 异动类型（同花顺 tag_name：大涨/快速拉升，仅当日有效）
    anomaly_content: str = ""  # 异动解读原文（同花顺 analysis_content，仅当日有效，过 0 点清空）

    @property
    def fengdan_mv_ratio(self) -> float:
        """封单/流通市值（%），龙头排序核心因子"""
        if self.float_mv <= 0:
            return 0.0
        return self.fengdan_amt / self.float_mv * 100


@dataclass
class ConceptBlock:
    """静态概念板块及其成分"""
    name: str                  # 概念名，如 "CPO概念"
    codes: list[str] = field(default_factory=list)
    source: str = ""           # 数据来源标记


class DataSource(ABC):
    """数据源接口。实现方需保证：失败时抛异常，由 router 负责降级。"""

    name: str = "base"

    @abstractmethod
    def is_available(self) -> bool:
        """探活：凭据/依赖是否就绪，网络是否可达"""
        ...

    @abstractmethod
    def get_zt_pool(self, trade_date: date) -> list[ZTStock]:
        """拉取指定交易日涨停池"""
        ...

    @abstractmethod
    def get_zt_pool_zhaban(self, trade_date: date) -> list[ZTStock]:
        """拉取炸板池（曾涨停未封住）。数据源不支持则返回空列表。"""
        ...

    @abstractmethod
    def get_concept_blocks(self) -> list[ConceptBlock]:
        """拉取全部静态概念板块成分（周更缓存用）"""
        ...

    def get_strong_pool(self, trade_date: date, exclude_codes=None) -> list[ZTStock]:
        """10cm 异动池：非主板（创业板/科创板/北交所）涨幅≥10% 且未封板的强势股。

        开盘啦口径：这类股虽未封板但涨幅已超主板一个涨停板，计入板块强度。
        数据源不支持则返回空列表（默认实现）。
        """
        return []


def normalize_code(raw: str) -> str:
    """各数据源代码格式归一：'000586.SZ' / 'sz000586' / '000586' -> '000586'"""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def is_bj_code(code: str) -> bool:
    """北交所代码段：43/83/87/88/92 开头"""
    return code[:2] in {"43", "83", "87", "88", "92"}
