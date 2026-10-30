"""数据源路由：按优先级尝试，失败自动降级 + 重试 + 日志

用法:
    router = DataSourceRouter(cfg)
    pool = router.get_zt_pool(date.today())   # 主源挂了自动切备源
"""
from __future__ import annotations

import time
from datetime import date

from src.config import Config
from src.datasources.base import ConceptBlock, DataSource, ZTStock
from src.log import get_logger

log = get_logger(__name__)

_SOURCE_REGISTRY = {
    "fuyao": "src.datasources.fuyao_ds.FuyaoSource",
    "ifind": "src.datasources.ifind_ds.IFindSource",
    "akshare": "src.datasources.akshare_ds.AkshareSource",
    "wencai": "src.datasources.wencai_ds.WencaiSource",
}


def _load_source(name: str) -> DataSource:
    import importlib

    module_path, cls_name = _SOURCE_REGISTRY[name].rsplit(".", 1)
    cls = getattr(importlib.import_module(module_path), cls_name)
    return cls()


class _NullSource:
    """占位数据源：一个都没配成时用它顶上。

    以前这里直接抛 RuntimeError，结果连「页面打开看历史数据」都做不到——
    服务器上先部署、后填 API Key 是很常见的顺序，不该因此整个站起不来。
    """
    name = "none"

    def __getattr__(self, item):
        raise RuntimeError(
            "没有可用的数据源：请在项目根目录 .env 里配置 HITHINK_FINANCE_API_KEY，然后重启服务"
        )


class DataSourceRouter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._sources: list[DataSource] = []
        for name in cfg.get("datasource.priority", ["akshare"]):
            if not cfg.get(f"datasource.{name}.enabled", True):
                continue
            try:
                self._sources.append(_load_source(name))
                log.info("数据源 [%s] 初始化成功", name)
            except Exception as e:
                log.warning("数据源 [%s] 初始化失败，跳过: %s", name, e)

        self._timeout = cfg.get("datasource.timeout_sec", 15)
        self._retry = cfg.get("datasource.retry", 2)
        self._interval = cfg.get("datasource.retry_interval_sec", 3)

        if not self._sources:
            log.warning("没有可用的数据源：页面可以打开并查看历史数据，但采集/复盘会失败")
            self.active = _NullSource()
        else:
            self.active: DataSource = self._sources[0]

    # ------------------------------------------------ 调度核心
    def _call(self, method: str, *args):
        if not self._sources:
            raise RuntimeError(
                "没有可用的数据源：请在项目根目录 .env 里配置 HITHINK_FINANCE_API_KEY，然后重启服务"
            )
        errors: list[str] = []
        for src in self._sources:
            fn = getattr(src, method)
            for attempt in range(1, self._retry + 1):
                try:
                    result = fn(*args)
                    if src is not self.active:
                        log.warning("数据源降级生效：%s -> %s（%s）",
                                    self.active.name, src.name, method)
                        self.active = src
                    return result
                except Exception as e:
                    errors.append(f"{src.name}: {e}")
                    log.warning("[%s] %s 第 %d 次失败: %s", src.name, method, attempt, e)
                    time.sleep(self._interval)
            log.error("数据源 [%s] %s 彻底失败，尝试降级", src.name, method)
        raise RuntimeError(f"所有数据源均失败: {'; '.join(errors)}")

    # ------------------------------------------------ 对外接口
    def get_zt_pool(self, trade_date: date) -> list[ZTStock]:
        return self._call("get_zt_pool", trade_date)

    def get_zt_pool_zhaban(self, trade_date: date) -> list[ZTStock]:
        return self._call("get_zt_pool_zhaban", trade_date)

    def get_concept_blocks(self) -> list[ConceptBlock]:
        return self._call("get_concept_blocks")

    def get_strong_pool(self, trade_date: date, exclude_codes=None) -> list[ZTStock]:
        return self._call("get_strong_pool", trade_date, exclude_codes)
