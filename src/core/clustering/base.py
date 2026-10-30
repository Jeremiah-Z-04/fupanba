"""聚类接口：MVP1 静态概念直通 / MVP2 规则版 / MVP3 HDBSCAN 都实现同一接口"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Clusterer(ABC):
    """输入涨停池个股列表，输出 {题材名: [个股dict, ...]}"""

    name: str = "base"

    @abstractmethod
    def cluster(self, stocks: list[dict], zhaban: list[dict]) -> dict[str, list[dict]]:
        ...
