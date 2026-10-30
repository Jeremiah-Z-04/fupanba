"""MVP1 聚类：静态概念直通

不做算法聚类，直接用概念板块成分表把当日涨停股归堆：
  个股可归属多个概念 → 会出现在多个簇中（导出时全局去重，保留最高分簇）
  簇内涨停数 < min_cluster_size 的概念被剔除（无效小簇）
"""
from __future__ import annotations

from src.config import Config
from src.core.clustering.base import Clusterer
from src.log import get_logger
from src.storage.snapshot_repo import SnapshotRepo

log = get_logger(__name__)


class StaticConceptClusterer(Clusterer):
    name = "static_concept"

    def __init__(self, cfg: Config, repo: SnapshotRepo):
        self.min_size = cfg.get("clustering.min_cluster_size", 2)
        self.repo = repo
        self.blacklist = set(
            cfg.get("clustering.generic_concept_blacklist", []) or []
        )

    def cluster(self, stocks: list[dict], zhaban: list[dict]) -> dict[str, list[dict]]:
        clusters: dict[str, list[dict]] = {}
        unlabeled = []
        for s in stocks:
            concepts = self.repo.concepts_of(s["code"])
            if not concepts:
                unlabeled.append(s["code"])
                continue
            for c in concepts:
                if c in self.blacklist:
                    continue
                clusters.setdefault(c, []).append(s)

        before = len(clusters)
        clusters = {k: v for k, v in clusters.items() if len(v) >= self.min_size}
        log.info(
            "静态概念聚类: %d 只涨停 -> %d 个概念簇（剔除小簇后 %d 个），无标签 %d 只",
            len(stocks), before, len(clusters), len(unlabeled),
        )
        if unlabeled:
            log.debug("无概念标签标的: %s", ",".join(unlabeled[:20]))
        return dict(sorted(clusters.items(), key=lambda kv: -len(kv[1])))
